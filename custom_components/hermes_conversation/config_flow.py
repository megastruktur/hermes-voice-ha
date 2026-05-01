"""Config flow for MARC-10 Hermes Conversation."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_ENDPOINT,
    CONF_MODEL,
    CONF_STRIP_EMOJI,
    CONF_TIMEOUT,
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    DEFAULT_STRIP_EMOJI,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_ENDPOINT, default=defaults.get(CONF_ENDPOINT, DEFAULT_ENDPOINT)
            ): str,
            vol.Required(CONF_API_KEY, default=defaults.get(CONF_API_KEY, "")): str,
            vol.Optional(
                CONF_MODEL, default=defaults.get(CONF_MODEL, DEFAULT_MODEL)
            ): str,
            vol.Optional(
                CONF_TIMEOUT, default=defaults.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
            ): int,
            vol.Optional(
                CONF_STRIP_EMOJI,
                default=defaults.get(CONF_STRIP_EMOJI, DEFAULT_STRIP_EMOJI),
            ): bool,
        }
    )


async def _validate_endpoint(
    hass, endpoint: str, api_key: str, timeout: int
) -> tuple[bool, str | None]:
    """Probe Hermes /health; return (ok, error_code_or_None)."""
    session = async_get_clientsession(hass)
    url = endpoint.rstrip("/") + "/health"
    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=aiohttp.ClientTimeout(total=min(10, max(2, timeout))),
        ) as resp:
            if resp.status == 401:
                return False, "invalid_auth"
            if resp.status >= 400:
                return False, "cannot_connect"
    except (aiohttp.ClientError, TimeoutError) as exc:
        _LOGGER.warning("Hermes health probe failed: %s", exc)
        return False, "cannot_connect"
    return True, None


class HermesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MARC-10."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            ok, err = await _validate_endpoint(
                self.hass,
                user_input[CONF_ENDPOINT],
                user_input.get(CONF_API_KEY, ""),
                user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
            )
            if not ok and err:
                errors["base"] = err
            else:
                # Normalize URL for unique_id so http://host:8642 and
                # http://host:8642/ aren't treated as different endpoints.
                normalized = user_input[CONF_ENDPOINT].rstrip("/").lower()
                await self.async_set_unique_id(normalized)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"MARC-10 ({user_input[CONF_ENDPOINT]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HermesOptionsFlow(config_entry)


class HermesOptionsFlow(OptionsFlow):
    """Allow editing endpoint, key, model, timeout after install."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            ok, err = await _validate_endpoint(
                self.hass,
                user_input[CONF_ENDPOINT],
                user_input.get(CONF_API_KEY, ""),
                user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
            )
            if not ok and err:
                errors["base"] = err
            else:
                # Persist by updating the entry data; reload triggered via
                # update_listener registered in __init__.async_setup_entry.
                self.hass.config_entries.async_update_entry(
                    self._entry, data=user_input
                )
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=_user_schema(user_input or dict(self._entry.data)),
            errors=errors,
        )
