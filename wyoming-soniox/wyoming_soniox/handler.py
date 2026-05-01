"""Wyoming event handler that forwards audio to Soniox real-time WebSocket STT.

Protocol semantics:
    - On the first AudioChunk we open a WebSocket to Soniox, send the JSON config,
      then start a background reader task that consumes Soniox responses and
      accumulates the text of every token whose is_final == True.
    - Every subsequent AudioChunk's PCM payload is forwarded as a binary frame.
    - On AudioStop we send {"type": "finalize"}, drain any remaining final
      tokens, close the socket, and emit one Wyoming Transcript event with the
      full accumulated text.
    - On Describe we return the cached Info event.

Audio is converted to 16 kHz / 16-bit / mono PCM (s16le) so we can match the
single config we send to Soniox.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import websockets
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.server import AsyncEventHandler

_LOGGER = logging.getLogger(__name__)

SONIOX_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


class SonioxHandler(AsyncEventHandler):
    """Per-connection event handler.

    A new instance is created by the Wyoming server for every TCP connection
    from Home Assistant. Instances must be cheap to construct and must clean
    up their network resources on disconnect.
    """

    def __init__(
        self,
        wyoming_info: Info,
        api_key: str,
        model: str,
        language_hints: list[str],
        max_endpoint_delay_ms: int,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self._wyoming_info_event = wyoming_info.event()
        self._api_key = api_key
        self._model = model
        self._language_hints = language_hints
        self._max_endpoint_delay_ms = max_endpoint_delay_ms

        # Wyoming clients send arbitrary sample rates; Soniox config is fixed,
        # so we resample everything to 16 kHz mono s16le on the fly.
        self._converter = AudioChunkConverter(rate=16000, width=2, channels=1)

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._final_text_parts: list[str] = []
        # Override language for this single transcription, set by Transcribe event.
        self._language_override: Optional[list[str]] = None

    # ------------------------------------------------------------------ Wyoming

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self._wyoming_info_event)
            _LOGGER.debug("Sent Info to client")
            return True

        if Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            if transcribe.language:
                self._language_override = [transcribe.language]
                _LOGGER.debug("Language override: %s", transcribe.language)
            return True

        if AudioStart.is_type(event.type):
            # Reset per-utterance state.
            self._final_text_parts = []
            return True

        if AudioChunk.is_type(event.type):
            chunk = self._converter.convert(AudioChunk.from_event(event))

            if self._ws is None:
                await self._open_soniox()

            assert self._ws is not None
            try:
                await self._ws.send(chunk.audio)
            except websockets.ConnectionClosed:
                _LOGGER.warning("Soniox WS closed while sending audio")
            return True

        if AudioStop.is_type(event.type):
            await self._finalize_and_emit()
            # Returning False signals the Wyoming server to close the
            # connection after this utterance, matching wyoming-faster-whisper
            # behaviour. HA opens a fresh connection per pipeline run.
            return False

        return True

    # ------------------------------------------------------------------- Soniox

    async def _open_soniox(self) -> None:
        """Open WS, send config, start background reader."""
        _LOGGER.debug("Opening Soniox WS")
        self._ws = await websockets.connect(
            SONIOX_WS_URL,
            max_size=2 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )

        config = {
            "api_key": self._api_key,
            "model": self._model,
            "audio_format": "pcm_s16le",
            "sample_rate": 16000,
            "num_channels": 1,
            "language_hints": self._language_override or self._language_hints,
            "enable_endpoint_detection": True,
            "max_endpoint_delay_ms": self._max_endpoint_delay_ms,
        }
        await self._ws.send(json.dumps(config))

        self._reader_task = asyncio.create_task(self._read_soniox())

    async def _read_soniox(self) -> None:
        """Background loop: read JSON responses, append final tokens."""
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    continue  # Soniox only sends text frames.
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    _LOGGER.warning("Non-JSON from Soniox: %r", message[:200])
                    continue

                if "error_code" in payload:
                    _LOGGER.error(
                        "Soniox error %s: %s",
                        payload.get("error_code"),
                        payload.get("error_message"),
                    )
                    continue

                tokens = payload.get("tokens") or []
                for token in tokens:
                    if not token.get("is_final"):
                        continue
                    text = token.get("text", "")
                    # Soniox emits service markers wrapped in angle brackets:
                    #   <end> — endpoint detection (segment boundary)
                    #   <fin> — response to our explicit {"type":"finalize"}
                    # Real transcript tokens never contain angle brackets, so
                    # filter any <...>-only token defensively.
                    stripped = text.strip()
                    if (
                        stripped.startswith("<")
                        and stripped.endswith(">")
                        and len(stripped) <= 16
                    ):
                        continue
                    self._final_text_parts.append(text)
        except websockets.ConnectionClosed:
            _LOGGER.debug("Soniox WS reader: connection closed")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Soniox WS reader crashed")

    async def _finalize_and_emit(self) -> None:
        """Send finalize, drain reader, close WS, emit Transcript."""
        text = ""
        try:
            if self._ws is not None:
                try:
                    # Force any pending non-final tokens to finalize.
                    await self._ws.send(json.dumps({"type": "finalize"}))
                    # Send empty binary frame — Soniox treats this as
                    # end-of-stream and closes the connection.
                    await self._ws.send(b"")
                except websockets.ConnectionClosed:
                    pass

            if self._reader_task is not None:
                try:
                    await asyncio.wait_for(self._reader_task, timeout=10.0)
                except asyncio.TimeoutError:
                    _LOGGER.warning("Soniox reader did not finish in 10s; cancelling")
                    self._reader_task.cancel()

            text = "".join(self._final_text_parts).strip()
        finally:
            await self._cleanup()

        _LOGGER.info("Transcript: %r", text)
        await self.write_event(Transcript(text=text).event())

    async def _cleanup(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self._reader_task = None
        self._language_override = None

    async def disconnect(self) -> None:
        await self._cleanup()
