"""Wyoming server entry point for the Soniox STT bridge."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from functools import partial

from wyoming.info import AsrModel, AsrProgram, Attribution, Info
from wyoming.server import AsyncServer

from . import __version__
from .handler import SonioxHandler

_LOGGER = logging.getLogger(__name__)

# Soniox advertises 60+ languages; this is the curated subset we expose to HA.
# HA only uses this list for the Assist pipeline language picker — the actual
# decoding is multilingual regardless.
SUPPORTED_LANGUAGES = [
    "ru", "en", "uk", "be", "pl", "de", "fr", "es", "it", "pt",
    "nl", "tr", "cs", "sk", "hu", "ro", "bg", "el", "fi", "sv",
    "no", "da", "ja", "ko", "zh", "ar", "he", "hi",
]


async def main() -> None:
    parser = argparse.ArgumentParser(prog="wyoming-soniox")
    parser.add_argument(
        "--uri",
        default="tcp://0.0.0.0:10300",
        help="Wyoming URI (default: tcp://0.0.0.0:10300)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SONIOX_API_KEY"),
        help="Soniox API key (or set SONIOX_API_KEY env var)",
    )
    parser.add_argument(
        "--model",
        default="stt-rt-v3",
        help="Soniox real-time model (default: stt-rt-v3)",
    )
    parser.add_argument(
        "--language-hints",
        default="ru,en",
        help="Comma-separated language hints (default: ru,en)",
    )
    parser.add_argument(
        "--max-endpoint-delay-ms",
        type=int,
        default=900,
        help="Soniox endpoint detection max delay in ms (500-3000, default: 900)",
    )
    parser.add_argument(
        "--terms",
        default="",
        help=(
            "Soniox context.terms — domain vocabulary for biased recognition. "
            "Semicolon-separated to allow commas inside terms. "
            "Example: 'Foshi;MARCO-10;включи свет в гостиной'"
        ),
    )
    parser.add_argument(
        "--terms-file",
        default=None,
        help=(
            "Path to a UTF-8 file with one term per line (lines starting with # ignored). "
            "Loaded in addition to --terms."
        ),
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

    language_hints = [s.strip() for s in args.language_hints.split(",") if s.strip()]

    # Assemble context.terms from --terms (inline, ;-separated) and --terms-file.
    context_terms: list[str] = []
    if args.terms:
        context_terms.extend(t.strip() for t in args.terms.split(";") if t.strip())
    if args.terms_file:
        from pathlib import Path
        path = Path(args.terms_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"--terms-file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                context_terms.append(line)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    context_terms = [t for t in context_terms if not (t in seen or seen.add(t))]

    wyoming_info = Info(
        asr=[
            AsrProgram(
                name="soniox",
                description="Soniox real-time multilingual STT (cloud)",
                attribution=Attribution(
                    name="Soniox",
                    url="https://soniox.com/",
                ),
                installed=True,
                version=__version__,
                models=[
                    AsrModel(
                        name=args.model,
                        description=f"Soniox {args.model}",
                        attribution=Attribution(
                            name="Soniox",
                            url="https://soniox.com/docs/stt/models",
                        ),
                        installed=True,
                        languages=SUPPORTED_LANGUAGES,
                        version=__version__,
                    )
                ],
            )
        ],
    )

    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info(
        "Ready: %s | model=%s | hints=%s | endpoint_delay=%dms | terms=%d",
        args.uri,
        args.model,
        ",".join(language_hints),
        args.max_endpoint_delay_ms,
        len(context_terms),
    )

    await server.run(
        partial(
            SonioxHandler,
            wyoming_info,
            args.api_key,
            args.model,
            language_hints,
            args.max_endpoint_delay_ms,
            context_terms,
        )
    )


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
