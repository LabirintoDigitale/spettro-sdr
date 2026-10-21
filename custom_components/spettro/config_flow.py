"""Configurazione guidata: indirizzo e porta del server rtl_tcp."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .const import DEFAULT_PORT, DOMAIN
from .scanner import RtlTcpError, ScanSettings, probe


class SpettroConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            try:
                tuner = await probe(host, port)
            except RtlTcpError:
                errors["base"] = "not_rtl_tcp"
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"SDR {host}:{port} ({tuner})",
                    data={CONF_HOST: host, CONF_PORT: port},
                    options=ScanSettings().as_dict(),
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
