# Hermes Voice for Home Assistant

Use [Hermes Agent](https://github.com/NousResearch/hermes-agent) as the conversation
agent in Home Assistant — talk to your full-power LLM agent (with tools, memory,
and skills) through HA Assist on the phone, satellites, or browser, with end-to-end
streaming for low first-word latency.

This monorepo contains three independent components that compose into a complete
voice loop:

```
Android HA app (mic)
    │
    ▼
HA Assist pipeline
    ├─► STT slot   ──Wyoming──►  wyoming-soniox       ──WS──►  Soniox cloud
    ├─► Conversation ──HTTP/SSE──►  Hermes api_server (streaming)
    └─► TTS slot   ──Wyoming──►  wyoming-soniox-tts   ──WS──►  Soniox cloud
```

| Component | What it is | Standalone-useful? |
|---|---|---|
| [`custom_components/hermes_conversation/`](./custom_components/hermes_conversation) | HA conversation entity that proxies to any OpenAI-compatible streaming endpoint (Hermes Agent, LiteLLM, vLLM, etc.) | ✅ Works with any OpenAI-shaped chat-completions API |
| [`wyoming-soniox/`](./wyoming-soniox) | Wyoming-protocol bridge to Soniox real-time WebSocket STT | ✅ Drop-in STT for any HA Assist pipeline |
| [`wyoming-soniox-tts/`](./wyoming-soniox-tts) | Wyoming-protocol bridge to Soniox real-time WebSocket TTS | ✅ Drop-in TTS for any HA Assist pipeline |

You can install all three together for a complete Hermes-powered voice assistant,
or pick just the Soniox bridges if you only want fast cloud STT/TTS in HA.

---

## Why this exists

Home Assistant's built-in conversation agents (Assist's local LLM, OpenAI extension,
etc.) are limited:

- No persistent memory across utterances
- No tool/function calling beyond HA's intent system
- No skills, no custom system prompts at runtime
- No streaming — the user waits for the entire reply before TTS starts

Hermes Agent has all of this. This repo wires Hermes into HA's voice pipeline so
**every voice command runs through the same agent you talk to in Telegram, Discord,
or the CLI** — same memory, same skills, same tools, same persona.

Soniox was chosen for STT/TTS because:

- Real-time WebSocket API with sub-second first-token / first-chunk latency
- Native multilingual handling (no per-language model swaps)
- Reasonable pricing
- Works without a VPN from most regions

You can swap either bridge for any other Wyoming-compatible STT/TTS without
touching the conversation integration.

---

## Quick start

### 1. Install the HA conversation integration

```bash
# On the host running Home Assistant
git clone https://github.com/<you>/hermes-voice-ha.git
cp -r hermes-voice-ha/custom_components/hermes_conversation \
      /path/to/homeassistant/config/custom_components/
```

If HA runs in Docker, bind-mount instead:

```yaml
# docker-compose.yml
services:
  homeassistant:
    volumes:
      - ./hermes-voice-ha/custom_components/hermes_conversation:/config/custom_components/hermes_conversation:ro
```

Restart HA, then **Settings → Devices & Services → Add Integration → Hermes
Conversation**. Provide:

- **Endpoint** — your Hermes Agent api_server URL (e.g. `http://192.168.1.10:8642`)
- **API key** — value of `API_SERVER_KEY` from your Hermes config
- **Model** — name of the Hermes profile (e.g. `Main`)
- **Timeout** — 60s is a sensible default

The integration registers a conversation entity (e.g. `conversation.hermes_conversation`)
that you can use as the conversation agent in any Assist pipeline.

### 2. Install the Soniox STT bridge

```bash
cd hermes-voice-ha/wyoming-soniox
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Set your Soniox API key in an env file:

```bash
echo "SONIOX_API_KEY=your-key-here" > ~/.soniox.env
chmod 600 ~/.soniox.env
```

Edit `marc-wyoming-soniox.service` to point at your env file and venv path, then:

```bash
mkdir -p ~/.config/systemd/user
cp marc-wyoming-soniox.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now marc-wyoming-soniox.service
```

Add it to HA: **Settings → Devices & Services → Add Integration → Wyoming Protocol**,
host = the machine running the bridge, port = `10300`.

### 3. Install the Soniox TTS bridge

Same pattern as STT:

```bash
cd hermes-voice-ha/wyoming-soniox-tts
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .

cp marc-wyoming-soniox-tts.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now marc-wyoming-soniox-tts.service
```

Add to HA via Wyoming Protocol on port `10400`.

### 4. Wire up the Assist pipeline

**Settings → Voice assistants → Add assistant**:

- Conversation agent: `Hermes Conversation`
- Speech-to-text: `Soniox` (the Wyoming integration you just added)
- Text-to-speech: `Soniox TTS`
- Pick a voice (e.g. `Adrian`)

Set as the default pipeline in the HA Android app: **Settings → Companion app →
Voice → Voice assistant**, pick your new pipeline.

Done. Hold the assist button on your phone and start talking.

---

## Component-specific docs

Each component has its own README with deeper detail:

- **[hermes_conversation README](./custom_components/hermes_conversation)** — config flow,
  streaming behavior, session pinning, troubleshooting
- **[wyoming-soniox README](./wyoming-soniox/README.md)** — Soniox config flags,
  endpoint detection tuning, language hints, standalone test client
- **[wyoming-soniox-tts README](./wyoming-soniox-tts/README.md)** — voice list,
  language auto-detection from text, char limits

---

## Architecture notes

### Streaming end-to-end

The integration requests `stream: true` from the Hermes api_server and feeds
OpenAI-shaped SSE deltas straight into HA's `chat_log.async_add_delta_content_stream`.
HA's TTS pipeline starts speaking the first sentence the moment Hermes emits it,
not after the full reply finishes generating. Tool calls execute server-side in
Hermes — no `tool_calls` deltas leak into HA, so the voice stream stays clean.

Requires Home Assistant 2025.8+ (for `async_add_delta_content_stream`).

### Session pinning

HA generates a fresh `conversation_id` per pipeline run, which would make every
voice utterance look like a brand-new conversation to Hermes. The integration
ignores HA's id and pins `session_id` to the config-entry id, so the agent keeps
context across all voice utterances from the same HA install.

### Soniox service tokens

Soniox emits `is_final: true` tokens whose text is wrapped in angle brackets
(`<end>`, `<fin>`, …) as control markers. The STT bridge filters any token of
shape `<...>` ≤ 16 chars defensively, so future markers don't leak into transcripts.

### Language detection in TTS

HA's Wyoming TTS client doesn't always send `voice.language`. The TTS bridge
falls back to script detection on the text (Cyrillic → `ru`, etc.) so Russian
replies don't get spoken with English phonemes.

---

## Requirements

- **Home Assistant** 2025.8 or newer (for streaming conversation deltas)
- **Python** 3.11+ on whatever host runs the Wyoming bridges
- **Hermes Agent** running with `api_server` enabled, OR any other OpenAI-compatible
  streaming chat-completions endpoint
- **Soniox API key** (free tier available at [soniox.com](https://soniox.com))

---

## Status

- ✅ HA conversation integration — production
- ✅ Wyoming Soniox STT bridge — production
- ✅ Wyoming Soniox TTS bridge — production
- ✅ End-to-end streaming verified (simple reply ~2s, tool-use reply ~5s to first audio)

## Related

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the agent runtime
  this integration is built around
- [Wyoming Protocol](https://github.com/rhasspy/wyoming) — the audio protocol HA
  uses for STT/TTS
- [Soniox](https://soniox.com) — the cloud STT/TTS provider

## License

MIT — see [LICENSE](./LICENSE).
