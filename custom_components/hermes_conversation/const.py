"""Constants for the MARC-10 Hermes Conversation integration."""

DOMAIN = "hermes_conversation"

CONF_ENDPOINT = "endpoint"
CONF_API_KEY = "api_key"
CONF_MODEL = "model"
CONF_TIMEOUT = "timeout"
CONF_STRIP_EMOJI = "strip_emoji"

DEFAULT_ENDPOINT = "http://192.168.3.23:8642"
# Confirmed via /v1/models in Phase 0 — Hermes advertises the active profile
# under this stable model id, not "Main".
DEFAULT_MODEL = "hermes-agent"
DEFAULT_TIMEOUT = 120  # Voice commands can trigger full agent runs (skill use,
                       # tool chains) that exceed simple chat latency. 60s causes
                       # spurious "MARC unreachable" on memory cleanup, search, etc.
# MARC personality auto-injects 🖥️ at the start of every reply.
# TTS engines either say "monitor emoji" or fail silently — strip by default.
DEFAULT_STRIP_EMOJI = True
