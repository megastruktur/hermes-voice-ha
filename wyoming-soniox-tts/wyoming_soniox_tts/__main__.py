"""Wyoming server entry point for the Soniox TTS bridge."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from functools import partial

from wyoming.info import Attribution, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncServer

from . import __version__
from .handler import SonioxTtsHandler

_LOGGER = logging.getLogger(__name__)

# Curated list of voices Soniox advertises for tts-rt-v1. We expose a few
# named speakers per major language so HA's "voice" dropdown isn't empty.
# The full voice catalog is much larger — extend as needed. Each entry is
# (voice_name, [language_codes_supported]). The voice name is what gets sent
# to Soniox in the WS config; the language list is what HA uses to filter.
DEFAULT_VOICES: list[tuple[str, list[str]]] = [
    # English
    ("Adrian", ["en"]),
    ("Maya", ["en"]),
    # Multilingual fallbacks — Soniox tts-rt-v1 is multilingual; the same
    # voice can speak multiple languages. We re-list "Adrian" under the langs
    # we want HA to surface in its picker.
    ("Adrian", ["ru", "be", "uk", "pl"]),
]

# Curated language list (Soniox advertises 60+; this is what shows in HA's
# pipeline language picker).
SUPPORTED_LANGUAGES = [
    "en", "ru", "be", "uk", "pl",
    "de", "fr", "es", "it", "pt",
    "nl", "tr", "cs", "sk", "hu", "ro", "bg", "el", "fi", "sv",
    "no", "da", "ja", "ko", "zh", "ar", "he", "hi",
]


async def main() -> None:
    parser = argparse.ArgumentParser(prog="wyoming-soniox-tts")
    parser.add_argument(
        "--uri",
        default="tcp://0.0.0.0:10400",
        help="Wyoming URI (default: tcp://0.0.0.0:10400 — standard TTS port)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SONIOX_API_KEY"),
        help="Soniox API key (or set SONIOX_API_KEY env var)",
    )
    parser.add_argument(
        "--model",
        default="tts-rt-v1",
        help="Soniox TTS model (default: tts-rt-v1)",
    )
    parser.add_argument(
        "--default-voice",
        default="Adrian",
        help="Default voice when HA doesn't request a specific one (default: Adrian)",
    )
    parser.add_argument(
        "--default-language",
        default="en",
        help="Default language when HA doesn't request one (default: en)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=24000,
        help="Output sample rate in Hz (default: 24000)",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.api_key:
        raise SystemExit(
            "SONIOX_API_KEY is missing — pass --api-key or set the env var"
        )

    # Build a unique TtsVoice per (voice_name, language_set) — Wyoming/HA
    # de-dupe on voice.name+languages. Group by voice name so HA's dropdown
    # shows each voice once with the union of its supported languages.
    voices_by_name: dict[str, set[str]] = {}
    for vname, langs in DEFAULT_VOICES:
        voices_by_name.setdefault(vname, set()).update(langs)

    voices = [
        TtsVoice(
            name=vname,
            description=f"Soniox {vname}",
            attribution=Attribution(
                name="Soniox",
                url="https://soniox.com/text-to-speech",
            ),
            installed=True,
            version=__version__,
            languages=sorted(vlangs),
        )
        for vname, vlangs in sorted(voices_by_name.items())
    ]

    wyoming_info = Info(
        tts=[
            TtsProgram(
                name="soniox-tts",
                description="Soniox real-time multilingual TTS (cloud)",
                attribution=Attribution(
                    name="Soniox",
                    url="https://soniox.com/",
                ),
                installed=True,
                version=__version__,
                voices=voices,
            )
        ],
    )

    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info(
        "Ready: %s | model=%s | default_voice=%s | default_lang=%s | sr=%d",
        args.uri,
        args.model,
        args.default_voice,
        args.default_language,
        args.sample_rate,
    )

    await server.run(
        partial(
            SonioxTtsHandler,
            wyoming_info,
            args.api_key,
            args.model,
            args.default_voice,
            args.default_language,
            args.sample_rate,
        )
    )


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
