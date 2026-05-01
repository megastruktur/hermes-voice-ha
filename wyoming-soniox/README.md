# wyoming-soniox

Wyoming protocol bridge for [Soniox](https://soniox.com/) real-time multilingual STT.
Lets Home Assistant Assist use Soniox as a streaming speech-to-text engine.

## Architecture

```
HA Assist  ──Wyoming(TCP)──►  wyoming-soniox  ──WS──►  stt-rt.soniox.com
```

- Per HA Assist run, HA opens one TCP connection.
- We open one Soniox WS connection per HA connection, stream PCM 16k/16/mono,
  collect final tokens, and emit a single `Transcript` event back to HA.
- Endpoint detection is on, so silence ends the utterance fast (~900 ms default).

## Run (dev)

```bash
cd ~/projects/marc-voice/wyoming-soniox
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
SONIOX_API_KEY=... python -m wyoming_soniox --debug
```

## systemd (production)

See `marc-wyoming-soniox.service` in this directory. Install with:

```bash
mkdir -p ~/.config/systemd/user
cp marc-wyoming-soniox.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now marc-wyoming-soniox.service
```

## Connect to HA

In Home Assistant: **Settings → Devices & Services → Add Integration → Wyoming Protocol**

- Host: `192.168.3.23` (or `127.0.0.1` if HA runs on the same host with host networking)
- Port: `10300`

Then in **Settings → Voice assistants → [your pipeline] → Speech-to-Text** pick
`soniox`.
