"""Tiny Wyoming client: send a WAV through wyoming-soniox and print the transcript."""

import asyncio
import sys
import wave

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient


async def main(uri: str, wav_path: str) -> int:
    with wave.open(wav_path, "rb") as wf:
        rate = wf.getframerate()
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())

    print(f"WAV: {rate} Hz, {width*8}-bit, {channels} ch, {len(frames)} bytes")

    host_port = uri.removeprefix("tcp://")
    host, port = host_port.split(":")

    async with AsyncTcpClient(host, int(port)) as client:
        await client.write_event(
            AudioStart(rate=rate, width=width, channels=channels).event()
        )

        # Send in 1024-sample chunks for realism.
        bytes_per_chunk = 1024 * width * channels
        for i in range(0, len(frames), bytes_per_chunk):
            await client.write_event(
                AudioChunk(
                    rate=rate, width=width, channels=channels,
                    audio=frames[i : i + bytes_per_chunk],
                ).event()
            )

        await client.write_event(AudioStop().event())
        print("Sent AudioStop, waiting for transcript...")

        while True:
            event = await client.read_event()
            if event is None:
                print("Connection closed without transcript")
                return 1
            if Transcript.is_type(event.type):
                t = Transcript.from_event(event)
                print(f"\n=== TRANSCRIPT ===\n{t.text}\n==================")
                return 0


if __name__ == "__main__":
    uri = sys.argv[1] if len(sys.argv) > 1 else "tcp://127.0.0.1:10399"
    wav = sys.argv[2] if len(sys.argv) > 2 else "/tmp/soniox_test.wav"
    sys.exit(asyncio.run(main(uri, wav)))
