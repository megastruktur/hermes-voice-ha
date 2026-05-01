"""MARC-10 conversation entity (streaming)."""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncGenerator, Literal

import aiohttp
from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
)
from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    AssistantContentDeltaDict,
    ChatLog,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_API_KEY,
    CONF_ENDPOINT,
    CONF_MODEL,
    CONF_STRIP_EMOJI,
    CONF_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_STRIP_EMOJI,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Compiled once. Covers the bulk of emoji + dingbats. MARC's signature 🖥️
# (U+1F5A5 + U+FE0F) lives in the supplementary plane → \U0001F000-\U0001FFFF.
# Variation selectors and zero-width joiners are stripped separately.
_EMOJI_RE = re.compile(
    r"["
    "\U0001F000-\U0001FFFF"  # Supplementary symbols & pictographs (incl. 🖥️)
    "\U00002600-\U000027BF"  # Misc symbols + dingbats (☀ ✓ ✗ etc.)
    "\U0000FE00-\U0000FE0F"  # Variation selectors
    "\U0000200D"             # Zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Remove emoji + variation selectors so TTS doesn't try to vocalize them."""
    return _EMOJI_RE.sub("", text).strip()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the MARC-10 conversation entity."""
    async_add_entities([HermesConversationEntity(hass, entry)])


class HermesConversationEntity(ConversationEntity):
    """A conversation entity that proxies to a Hermes Agent api_server.

    Phase 7 streaming: requests `stream:true` from Hermes, parses the SSE
    stream, and feeds OpenAI-shaped deltas into HA's chat_log via
    `async_add_delta_content_stream`. With a streaming-capable TTS engine
    (HA Cloud, Piper, or a wyoming bridge) the first phrase plays within
    ~0.5–1s of the first token; without one, HA buffers and speaks the
    full response — same UX as before, but the path is now ready for
    streaming TTS the moment we wire it up.
    """

    _attr_has_entity_name = True
    _attr_name = "MARC-10"
    # Declare CONTROL so HA exposes this entity to Assist's device-control
    # flow (lets users target it as their default conversation agent in
    # voice pipelines that need to control entities). Hermes itself does
    # NOT execute HA intents directly — it returns natural-language text;
    # but several HA UI flows gate on this feature being declared.
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        # Shared aiohttp session — DO NOT instantiate per request (HA anti-pattern).
        self._session = async_get_clientsession(hass)

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """MARC mirrors the speaker's language; accept all."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Forward the utterance to Hermes (streaming) and return the spoken reply."""
        endpoint = self._entry.data[CONF_ENDPOINT].rstrip("/")
        api_key = self._entry.data[CONF_API_KEY]
        model = self._entry.data.get(CONF_MODEL, DEFAULT_MODEL)
        timeout = self._entry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        strip_emoji = self._entry.data.get(CONF_STRIP_EMOJI, DEFAULT_STRIP_EMOJI)

        # Stable per-entry session (HA's per-run ULID would break voice context).
        session_id = f"ha-{self._entry.entry_id}"

        # Voice-mode behavioural injection. HA voice has a hard timeout;
        # MARC's normal agent loop (tool use, skills, RAG) routinely exceeds that.
        # We prepend a system-style hint via the user message channel — the
        # api_server doesn't currently honor a separate system role for stateful
        # sessions, so we inline it. Cheap, idempotent, no protocol risk.
        voice_directive = (
            "[VOICE MODE — HA Assist | spoken reply expected]\n"
            "Reply briefly (1-3 sentences). For long operations: schedule via cron "
            "or run in background, then confirm with one sentence. Skip preambles, "
            "tables, and code blocks — this is spoken aloud. No emoji.\n\n"
            "User: " + user_input.text
        )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": voice_directive}],
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Hermes-Session-Id": session_id,
        }

        response = intent.IntentResponse(language=user_input.language)
        agent_id = user_input.agent_id or self.entity_id

        try:
            async with self._session.post(
                f"{endpoint}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                resp.raise_for_status()

                # Drain Hermes SSE → push deltas into chat_log → collect full text
                # for the non-streaming-TTS fallback (response.async_set_speech).
                full_text = ""
                async for content in chat_log.async_add_delta_content_stream(
                    agent_id, _hermes_sse_to_deltas(resp)
                ):
                    if isinstance(content, AssistantContent) and content.content:
                        full_text += content.content

            if strip_emoji:
                full_text = _strip_emoji(full_text)
            if not full_text:
                full_text = "..."  # avoid empty TTS payloads
            response.async_set_speech(full_text)

        except aiohttp.ClientResponseError as exc:
            _LOGGER.error("Hermes returned HTTP %s: %s", exc.status, exc.message)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"MARC-10 returned an error: HTTP {exc.status}",
            )
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.exception("Hermes connection failed")
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"MARC-10 unreachable: {exc}",
            )
        except (KeyError, IndexError, ValueError) as exc:
            _LOGGER.exception("Malformed Hermes response")
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"MARC-10 returned a malformed reply: {exc}",
            )

        return ConversationResult(
            response=response,
            conversation_id=session_id,
            continue_conversation=False,
        )


async def _hermes_sse_to_deltas(
    resp: aiohttp.ClientResponse,
) -> AsyncGenerator[AssistantContentDeltaDict, None]:
    """Translate Hermes' OpenAI-compatible SSE into HA's delta dict shape.

    Hermes streams chunks like:
        data: {"choices":[{"delta":{"role":"assistant"}, ...}]}
        data: {"choices":[{"delta":{"content":"hi"}, ...}]}
        data: [DONE]

    We yield each non-empty `delta` dict as-is. HA's
    `async_add_delta_content_stream` understands the OpenAI shape natively
    (role/content/tool_calls keys). Tool calls are executed server-side by
    Hermes and never appear in the stream — we only see content deltas.

    Buffering is required because aiohttp may deliver TCP chunks that split
    a single SSE event mid-line. We accumulate until we see `\\n\\n` (end of
    event), then process line-by-line.
    """
    buffer = ""
    async for raw in resp.content.iter_any():
        if not raw:
            continue
        buffer += raw.decode("utf-8", errors="replace")

        # SSE events are separated by a blank line. Process completed events
        # only; keep the trailing partial in the buffer.
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            for line in event.splitlines():
                if not line.startswith("data:"):
                    continue  # ignore comments, retry hints, event: lines
                payload = line[5:].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    return
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    _LOGGER.debug("Skipping malformed SSE chunk: %r", payload)
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if not delta:
                    continue  # empty deltas (final chunk) carry no new content

                yield delta  # type: ignore[misc]
