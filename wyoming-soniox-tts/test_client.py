"""Standalone Wyoming TTS client to probe wyoming-soniox-tts.

Usage:
    python test_client.py tcp://127.0.0.1:10400 "Hello from Soniox via Wyoming." [out.wav]

Sends a Synthesize event, drains audio chunks, optionally saves to WAV.
"""

from __future__ import annotations

import asyncio
import sys
import wave

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    uri = sys.argv[1]
    text = sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 else None

    voice = sys.argv[4] if len(sys.argv) > 4 else "Adrian"
    language = sys.argv[5] if len(sys.argv) > 5 else "en"

    print(f"connecting to {uri}")
    print(f"text: {text!r}  voice={voice} lang={language}")

    audio_buf = bytearray()
    sample_rate = width = channels = None
    chunks = 0
    t0 = asyncio.get_event_loop().time()
    first_chunk_t: float | None = None

    async with AsyncTcpClient.from_uri(uri) as client:
        await client.write_event(
            Synthesize(
                text=text,
                voice=SynthesizeVoice(name=voice, language=language),
            ).event()
        )

        while True:
            event = await asyncio.wait_for(client.read_event(), timeout=30)
            if event is None:
                print("connection closed")
                break

            if AudioStart.is_type(event.type):
                a = AudioStart.from_event(event)
                sample_rate, width, channels = a.rate, a.width, a.channels
                print(f"AudioStart: rate={a.rate} width={a.width} channels={a.channels}")
            elif AudioChunk.is_type(event.type):
                c = AudioChunk.from_event(event)
                audio_buf.extend(c.audio)
                chunks += 1
                if first_chunk_t is None:
                    first_chunk_t = asyncio.get_event_loop().time() - t0
                    print(f"first AudioChunk: {first_chunk_t:.3f}s, {len(c.audio)} bytes")
            elif AudioStop.is_type(event.type):
                print(f"AudioStop after {chunks} chunks, {len(audio_buf)} bytes total")
                break
            else:
                print(f"unexpected event: {event.type}")

    duration_s = (asyncio.get_event_loop().time() - t0)
    if sample_rate and width:
        speech_s = len(audio_buf) / (sample_rate * width * (channels or 1))
        print(f"OK: {speech_s:.2f}s of speech generated in {duration_s:.2f}s wall")
        print(f"   first-chunk latency: {first_chunk_t:.3f}s" if first_chunk_t else "   no audio received")

    if out_path and audio_buf and sample_rate:
        with wave.open(out_path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(width)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(audio_buf))
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
