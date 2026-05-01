"""Wyoming handler for Soniox real-time TTS WebSocket API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.server import AsyncEventHandler
from wyoming.tts import Synthesize

# Cap on text length sent in a single Synthesize. Soniox doesn't publish a
# hard limit but multi-paragraph dumps can stall the stream and make HA
# wait. 4000 chars ≈ 30s of speech — far longer than any voice reply
# should ever be. Truncate with ellipsis if exceeded.
MAX_TTS_CHARS = 4000

# Per-recv timeout. Soniox typically responds within 1-2s of the previous
# chunk; 30s gives massive headroom for transient cloud lag without letting
# a dead WS hang the pipeline forever.
WS_RECV_TIMEOUT_S = 30.0

# Connection timeout for the initial WS handshake to Soniox.
WS_CONNECT_TIMEOUT_S = 10.0


_LOGGER = logging.getLogger(__name__)

SONIOX_TTS_WS_URI = "wss://tts-rt.soniox.com/tts-websocket"

# Soniox TTS emits PCM little-endian 16-bit mono. Width = 2 bytes/sample.
PCM_WIDTH = 2
PCM_CHANNELS = 1

# Unicode block ranges for cheap script detection. Used when HA does NOT
# pass `voice.language` (the Wyoming HA integration drops it as of HA 2026.2 —
# only `name` and `speaker` are forwarded). Without this, every utterance
# falls back to `--default-language` and Adrian speaks Russian with English
# phonemes.
_SCRIPT_RANGES: list[tuple[str, range]] = [
    ("ru", range(0x0400, 0x0500)),   # Cyrillic block (covers ru/be/uk/bg/sr)
    ("ar", range(0x0600, 0x0700)),   # Arabic
    ("he", range(0x0590, 0x0600)),   # Hebrew
    ("zh", range(0x4E00, 0x9FFF)),   # CJK unified ideographs
    ("ja", range(0x3040, 0x30FF)),   # Hiragana + Katakana
    ("ko", range(0xAC00, 0xD7AF)),   # Hangul syllables
    ("hi", range(0x0900, 0x0980)),   # Devanagari
    ("th", range(0x0E00, 0x0E80)),   # Thai
]


def _detect_language_from_text(text: str, fallback: str) -> str:
    """Return language code based on dominant Unicode script in text.

    Counts characters per script; returns the script with the most hits if
    it beats Latin, else returns `fallback` (typically the configured default).
    Cheap and robust enough for choosing between en/ru/etc for TTS — we don't
    need actual language detection, just the right phoneme set.
    """
    counts: dict[str, int] = {"latin": 0}
    for ch in text:
        cp = ord(ch)
        if 0x0041 <= cp <= 0x024F:  # Basic + Latin Extended A/B
            counts["latin"] += 1
            continue
        for lang, rng in _SCRIPT_RANGES:
            if cp in rng:
                counts[lang] = counts.get(lang, 0) + 1
                break
    # Find dominant non-latin script
    non_latin = {k: v for k, v in counts.items() if k != "latin" and v > 0}
    if non_latin:
        winner = max(non_latin.items(), key=lambda kv: kv[1])
        # Only override fallback if the winning script has at least 1/3 of
        # the latin count (avoids a stray cyrillic letter flipping language).
        if winner[1] >= max(1, counts["latin"] // 3):
            return winner[0]
    return fallback


class SonioxTtsHandler(AsyncEventHandler):
    """One Wyoming TCP connection per HA Assist TTS request.

    Lifecycle:
      HA → Describe              → bridge replies with Info
      HA → Synthesize(text,voice)→ bridge opens Soniox WS, requests synthesis
      bridge → AudioStart         (declares pcm/24k/16/1)
      bridge → AudioChunk × N     (forwards each WS audio payload as-is)
      bridge → AudioStop          (after `terminated:true` from Soniox)
      return False                (HA closes the TCP connection)
    """

    def __init__(
        self,
        wyoming_info: Info,
        api_key: str,
        model: str,
        default_voice: str,
        default_language: str,
        sample_rate: int,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._wyoming_info_event = wyoming_info.event()
        self._api_key = api_key
        self._model = model
        self._default_voice = default_voice
        self._default_language = default_language
        self._sample_rate = sample_rate

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self._wyoming_info_event)
            return True

        if Synthesize.is_type(event.type):
            synth = Synthesize.from_event(event)
            text = (synth.text or "").strip()
            if not text:
                _LOGGER.warning("Synthesize event with empty text — skipping")
                # MUST emit AudioStart/Stop pair even on empty input or HA hangs
                await self._emit_silence()
                return False

            if len(text) > MAX_TTS_CHARS:
                _LOGGER.warning(
                    "Text length %d exceeds cap %d — truncating",
                    len(text), MAX_TTS_CHARS,
                )
                text = text[:MAX_TTS_CHARS - 1].rstrip() + "…"

            voice_name = self._default_voice
            language = None
            if synth.voice:
                if synth.voice.name:
                    voice_name = synth.voice.name
                if synth.voice.language:
                    language = synth.voice.language

            # HA's Wyoming TTS client (as of HA 2026.2) does NOT forward
            # voice.language — only name and speaker. Without this fallback,
            # Adrian speaks every Russian utterance with English phonemes.
            if not language:
                language = _detect_language_from_text(text, self._default_language)
                _LOGGER.debug(
                    "voice.language not provided by HA, detected from text: %s",
                    language,
                )

            try:
                await self._stream_tts(text, voice_name, language)
            except Exception:
                _LOGGER.exception("Soniox TTS stream failed")
                # Always close the audio frame — HA will hang forever otherwise
                await self._emit_silence()
            return False  # close TCP after one synth

        # Unknown event — ignore but keep connection (HA may probe with other types)
        return True

    async def _stream_tts(self, text: str, voice: str, language: str) -> None:
        """Open Soniox WS, stream a single synthesis, forward audio to HA."""
        stream_id = f"ha-{uuid.uuid4().hex[:12]}"
        _LOGGER.info(
            "TTS request: voice=%s lang=%s len=%d sid=%s",
            voice, language, len(text), stream_id,
        )

        config: dict[str, Any] = {
            "api_key": self._api_key,
            "model": self._model,
            "language": language,
            "voice": voice,
            "audio_format": "pcm_s16le",
            "sample_rate": self._sample_rate,
            "stream_id": stream_id,
        }

        audio_started = False
        bytes_total = 0
        chunks_sent = 0

        try:
            ws = await asyncio.wait_for(
                websockets.connect(SONIOX_TTS_WS_URI, max_size=None),
                timeout=WS_CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _LOGGER.error("Soniox WS handshake timed out after %ss", WS_CONNECT_TIMEOUT_S)
            raise
        except WebSocketException as exc:
            _LOGGER.error("Soniox WS handshake failed: %s", exc)
            raise

        async with ws:
            await ws.send(json.dumps(config))
            await ws.send(json.dumps({
                "text": text,
                "text_end": True,
                "stream_id": stream_id,
            }))

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=WS_RECV_TIMEOUT_S)
                except asyncio.TimeoutError:
                    _LOGGER.error(
                        "Soniox WS recv timeout (%ss) after %d chunks",
                        WS_RECV_TIMEOUT_S, chunks_sent,
                    )
                    break
                except ConnectionClosed as exc:
                    _LOGGER.warning(
                        "Soniox WS closed mid-stream (code=%s reason=%s) after %d chunks",
                        exc.code, exc.reason, chunks_sent,
                    )
                    break

                # All Soniox responses are JSON text frames
                try:
                    msg = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    _LOGGER.warning("Skipping non-JSON Soniox frame")
                    continue

                if "error_code" in msg:
                    _LOGGER.error(
                        "Soniox TTS error %s: %s",
                        msg.get("error_code"),
                        msg.get("error_message"),
                    )
                    break

                if msg.get("audio"):
                    pcm = base64.b64decode(msg["audio"])
                    if not audio_started:
                        await self.write_event(
                            AudioStart(
                                rate=self._sample_rate,
                                width=PCM_WIDTH,
                                channels=PCM_CHANNELS,
                            ).event()
                        )
                        audio_started = True
                    await self.write_event(
                        AudioChunk(
                            rate=self._sample_rate,
                            width=PCM_WIDTH,
                            channels=PCM_CHANNELS,
                            audio=pcm,
                        ).event()
                    )
                    bytes_total += len(pcm)
                    chunks_sent += 1

                if msg.get("terminated"):
                    break

        # Ensure AudioStart was emitted even if Soniox returned zero audio,
        # otherwise HA's media player gets confused.
        if not audio_started:
            await self._emit_silence()
        else:
            await self.write_event(AudioStop().event())
            duration_s = bytes_total / (self._sample_rate * PCM_WIDTH)
            _LOGGER.info(
                "TTS done: %d chunks, %d bytes, ~%.2fs of speech",
                chunks_sent, bytes_total, duration_s,
            )

    async def _emit_silence(self) -> None:
        """Emit a minimal valid AudioStart/Stop pair so HA doesn't hang."""
        await self.write_event(
            AudioStart(
                rate=self._sample_rate,
                width=PCM_WIDTH,
                channels=PCM_CHANNELS,
            ).event()
        )
        await self.write_event(AudioStop().event())
