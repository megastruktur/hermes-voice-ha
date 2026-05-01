# wyoming-soniox-tts

Wyoming protocol bridge that exposes [Soniox real-time TTS](https://soniox.com/text-to-speech) to Home Assistant Assist pipelines.

Companion to [`wyoming-soniox`](../wyoming-soniox) (STT direction) — together they form a single-vendor speech stack: ~$0.82/h per active conversation, 60+ languages, BY-accessible.

## Architecture

```
HA Assist pipeline ──Wyoming(TCP :10400)──► wyoming-soniox-tts ──WSS──► tts-rt.soniox.com
                                            (systemd user service)
```

One TCP connection per HA Synthesize request. Bridge opens a fresh Soniox WebSocket, forwards audio chunks back to HA as Wyoming `AudioChunk` events (PCM 24kHz/16-bit/mono).

## Install

```bash
cd ~/projects/marc-voice/wyoming-soniox-tts
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Requires `SONIOX_API_KEY` in `~/.hermes/.env`.

## Run (systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cp marc-wyoming-soniox-tts.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now marc-wyoming-soniox-tts.service
journalctl --user -u marc-wyoming-soniox-tts.service -f
```

Listens on `tcp://0.0.0.0:10400` (standard Wyoming TTS port).

## Wire into Home Assistant

Settings → Devices & Services → **Add Integration → Wyoming Protocol** → Host: host IP, Port: `10400`. Title appears as `soniox-tts`, exposes `tts.soniox_tts` entity.

Then in **Voice Assistants → MARC-10 (text)** pipeline, set `Text-to-speech` → `tts.soniox_tts` and a voice (e.g. `Adrian`).

## Standalone test

```bash
source venv/bin/activate
python3 test_client.py tcp://127.0.0.1:10400 "Hello captain." /tmp/out.wav Adrian en
python3 test_client.py tcp://127.0.0.1:10400 "Привет, капитан." /tmp/out-ru.wav Adrian ru
```

Output: WAV PCM 24kHz/16-bit/mono.

## Quirks discovered

- **HA does NOT forward `voice.language`.** As of HA 2026.2 the Wyoming TTS client constructs `SynthesizeVoice(name=..., speaker=...)` and drops the language entirely. Without a fallback, every request lands on `--default-language` and Adrian speaks Russian with English phonemes. The handler runs cheap script-based detection (Cyrillic block → `ru`, etc.) on the text when no language is provided. See `_detect_language_from_text` in `handler.py`.
- **Soniox WS is TEXT frames all the way down**, including the audio payload (which is base64'd inside the JSON). No binary frames in either direction. Don't try to read raw bytes.
- **Three-step termination handshake**: client sends `text_end:true`, server replies with last audio chunk + `audio_end:true`, then sends `terminated:true`. Don't close on `audio_end` alone — wait for `terminated`.
- **Adrian is multilingual** — same voice handles EN/RU/BE/UK/PL with reasonable quality. Each `(voice, language)` permutation in `DEFAULT_VOICES` becomes a separate row in HA's voice picker; the bridge groups by voice name in `Info` so the dropdown stays clean.
- **PCM rate locked at 24000 Hz** (Soniox default). Other rates accepted but untested. Width 2 bytes, mono.

## Args

```
--uri tcp://0.0.0.0:10400      Wyoming listen URI
--api-key                      Soniox API key (env: SONIOX_API_KEY)
--model tts-rt-v1              Soniox TTS model
--default-voice Adrian         Voice when HA doesn't request one
--default-language en          Language fallback (only when script detector returns latin-only)
--sample-rate 24000            Output PCM sample rate
--debug                        Verbose logging
```
