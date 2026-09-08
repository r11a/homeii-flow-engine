"""Config flow for HOMEii Flow Engine."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ENABLE_EXPERIMENTAL,
    CONF_INSTANCE_ID,
    CONF_MUSIC_ASSISTANT_EXTERNAL_URL,
    CONF_MUSIC_ASSISTANT_TOKEN,
    CONF_MUSIC_ASSISTANT_URL,
    CONF_PROFILE_ID,
    DEFAULT_INSTANCE_ID,
    DEFAULT_NAME,
    DEFAULT_PROFILE_ID,
    DOMAIN,
    MUSIC_ASSISTANT_SCHEMA_MIN,
)

MEDIA_TYPES = {
    "music": "Music",
    "playlist": "Playlist",
    "album": "Album",
    "artist": "Artist",
    "track": "Track",
    "radio": "Radio",
}

MENU_OPTIONS = {
    "general": "General settings",
    "add_volume_rule": "Add volume rule",
    "delete_volume_rule": "Delete volume rule",
    "add_schedule": "Add schedule",
    "delete_schedule": "Delete schedule",
    "run_schedule": "Run schedule now",
    "add_timer": "Add timer",
    "delete_timer": "Delete timer",
    "run_orchestration": "Run orchestration now",
    "done": "Done",
}


async def _validate_music_assistant_api(hass: Any, url: str, token: str) -> str | None:
    """Validate the required Music Assistant 2.10 API connection."""
    if not url or not token:
        return "required"
    base_url = url.rstrip("/")
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{base_url}/info",
            headers={"Accept": "application/json"},
            timeout=ClientTimeout(total=8, connect=4),
        ) as response:
            if response.status >= 400:
                return "cannot_connect"
            info = await response.json(content_type=None)
        if not isinstance(info, dict):
            return "invalid_response"
        schema_version = int(
            info.get("schema_version")
            or info.get("api_schema_version")
            or info.get("api_schema")
            or 0
        )
        if schema_version < MUSIC_ASSISTANT_SCHEMA_MIN:
            return "unsupported_ma_version"
        async with session.post(
            f"{base_url}/api",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "message_id": "homeii_config_check",
                "command": "players/all",
                "args": {
                    "return_unavailable": True,
                    "return_disabled": False,
                    "return_protocol_players": False,
                },
            },
            timeout=ClientTimeout(total=10, connect=4),
        ) as response:
            if response.status in (401, 403):
                return "invalid_auth"
            if response.status >= 400:
                return "cannot_connect"
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict) or payload.get("error_code") or payload.get("error"):
            return "invalid_auth" if "auth" in str(payload).lower() else "invalid_response"
    except (ClientError, TimeoutError, ValueError, TypeError):
        return "cannot_connect"
    return None


def _runtime_from_hass(hass: Any) -> Any | None:
    """Return the loaded runtime, if available."""
    runtime = hass.data.get(DOMAIN, {}).get("runtime")
    if runtime is not None and hasattr(runtime, "music_assistant_players_snapshot"):
        return runtime
    return None


def _profile_id(config_entry: config_entries.ConfigEntry) -> str:
    """Return the selected profile id."""
    return str(
        config_entry.options.get(CONF_PROFILE_ID)
        or config_entry.data.get(CONF_PROFILE_ID)
        or DEFAULT_PROFILE_ID
    )


def _parse_days(value: Any) -> list[int]:
    """Parse comma separated weekday numbers where Sunday is 0."""
    if value in (None, ""):
        return []
    days: list[int] = []
    for raw_part in str(value).replace(" ", ",").split(","):
        part = raw_part.strip()
        if not part:
            continue
        day = int(part)
        if not 0 <= day <= 6:
            raise ValueError("days must be between 0 and 6")
        days.append(day)
    return days


def _player_choices(runtime: Any | None) -> dict[str, str]:
    """Return Music Assistant player choices for config forms."""
    if runtime is None:
        return {}
    players = sorted(
        runtime.music_assistant_players_snapshot(),
        key=lambda item: str(item.get("friendly_name") or item.get("entity_id") or ""),
    )
    return {
        str(player["entity_id"]): f"{player.get('friendly_name') or player['entity_id']} ({player['entity_id']})"
        for player in players
    }


def _schedule_choices(runtime: Any | None, profile_id: str) -> dict[str, str]:
    """Return schedule choices."""
    if runtime is None:
        return {}
    choices: dict[str, str] = {}
    for schedule in runtime.schedules(profile_id):
        schedule_id = str(schedule.get("id") or "")
        if not schedule_id:
            continue
        name = schedule.get("name") or schedule_id
        player = schedule.get("player") or "no player"
        schedule_time = schedule.get("time") or "no time"
        choices[schedule_id] = f"{name} - {player} at {schedule_time}"
    return choices


def _volume_rule_choices(runtime: Any | None, profile_id: str) -> dict[str, str]:
    """Return volume rule choices."""
    if runtime is None:
        return {}
    choices: dict[str, str] = {}
    for rule in runtime.volume_rules(profile_id):
        player = str(rule.get("player") or "")
        if not player:
            continue
        max_volume = rule.get("max_volume")
        window = "always"
        if rule.get("start_time") or rule.get("end_time"):
            window = f"{rule.get('start_time') or '*'}-{rule.get('end_time') or '*'}"
        choices[player] = f"{player} - max {max_volume}% ({window})"
    return choices


def _timer_choices(runtime: Any | None, profile_id: str) -> dict[str, str]:
    """Return timer choices."""
    if runtime is None:
        return {}
    choices: dict[str, str] = {}
    for timer in runtime.timers(profile_id):
        timer_id = str(timer.get("id") or "")
        if not timer_id:
            continue
        player = timer.get("player") or "no player"
        action = timer.get("action") or "stop"
        ends_at = timer.get("ends_at") or "no time"
        choices[timer_id] = f"{player} - {action} at {ends_at}"
    return choices


class HomeiiFlowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a HOMEii Flow Engine config flow."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            instance_id = str(user_input.get(CONF_INSTANCE_ID) or DEFAULT_INSTANCE_ID).strip()
            music_assistant_url = str(
                user_input.get(CONF_MUSIC_ASSISTANT_URL) or ""
            ).strip()
            music_assistant_external_url = str(
                user_input.get(CONF_MUSIC_ASSISTANT_EXTERNAL_URL) or ""
            ).strip()
            music_assistant_token = str(
                user_input.get(CONF_MUSIC_ASSISTANT_TOKEN) or ""
            ).strip()
            if not instance_id:
                errors[CONF_INSTANCE_ID] = "required"
            if music_assistant_url and not music_assistant_url.startswith(
                ("http://", "https://")
            ):
                errors[CONF_MUSIC_ASSISTANT_URL] = "invalid_url"
            if music_assistant_external_url and not music_assistant_external_url.startswith("https://"):
                errors[CONF_MUSIC_ASSISTANT_EXTERNAL_URL] = "invalid_external_url"
            if not music_assistant_url:
                errors[CONF_MUSIC_ASSISTANT_URL] = "required"
            if not music_assistant_token:
                errors[CONF_MUSIC_ASSISTANT_TOKEN] = "required"
            if not errors:
                connection_error = await _validate_music_assistant_api(
                    self.hass, music_assistant_url, music_assistant_token
                )
                if connection_error:
                    errors["base"] = connection_error
            if not errors:
                await self.async_set_unique_id(instance_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=str(user_input.get("name") or DEFAULT_NAME),
                    data={
                        CONF_INSTANCE_ID: instance_id,
                        CONF_PROFILE_ID: str(user_input.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID).strip()
                        or DEFAULT_PROFILE_ID,
                        CONF_MUSIC_ASSISTANT_URL: music_assistant_url,
                        CONF_MUSIC_ASSISTANT_EXTERNAL_URL: music_assistant_external_url,
                        CONF_MUSIC_ASSISTANT_TOKEN: music_assistant_token,
                    },
                    options={
                        CONF_ENABLE_EXPERIMENTAL: bool(user_input.get(CONF_ENABLE_EXPERIMENTAL, False)),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional("name", default=DEFAULT_NAME): str,
                    vol.Optional(CONF_INSTANCE_ID, default=DEFAULT_INSTANCE_ID): str,
                    vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
                    vol.Required(CONF_MUSIC_ASSISTANT_URL, default=""): str,
                    vol.Optional(CONF_MUSIC_ASSISTANT_EXTERNAL_URL, default=""): str,
                    vol.Required(CONF_MUSIC_ASSISTANT_TOKEN, default=""): str,
                    vol.Optional(CONF_ENABLE_EXPERIMENTAL, default=False): bool,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return HomeiiFlowOptionsFlow(config_entry)


class HomeiiFlowOptionsFlow(config_entries.OptionsFlow):
    """Handle HOMEii Flow Engine options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the Engine management action selector."""
        if user_input is not None:
            action = str(user_input.get("action") or "done")
            if action == "done":
                return self.async_create_entry(title="", data=dict(self._config_entry.options))
            if action == "run_orchestration":
                return await self.async_step_run_orchestration()
            if action == "general":
                return await self.async_step_general()
            if action == "add_volume_rule":
                return await self.async_step_add_volume_rule()
            if action == "delete_volume_rule":
                return await self.async_step_delete_volume_rule()
            if action == "add_schedule":
                return await self.async_step_add_schedule()
            if action == "delete_schedule":
                return await self.async_step_delete_schedule()
            if action == "run_schedule":
                return await self.async_step_run_schedule()
            if action == "add_timer":
                return await self.async_step_add_timer()
            if action == "delete_timer":
                return await self.async_step_delete_timer()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("action", default="general"): vol.In(MENU_OPTIONS)}),
        )

    async def async_step_general(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage general options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            music_assistant_url = str(
                user_input.get(CONF_MUSIC_ASSISTANT_URL) or ""
            ).strip()
            music_assistant_external_url = str(
                user_input.get(CONF_MUSIC_ASSISTANT_EXTERNAL_URL) or ""
            ).strip()
            entered_token = str(user_input.get(CONF_MUSIC_ASSISTANT_TOKEN) or "").strip()
            existing_token = str(
                self._config_entry.options.get(CONF_MUSIC_ASSISTANT_TOKEN)
                or self._config_entry.data.get(CONF_MUSIC_ASSISTANT_TOKEN)
                or ""
            ).strip()
            effective_token = entered_token or existing_token
            if music_assistant_url and not music_assistant_url.startswith(
                ("http://", "https://")
            ):
                errors[CONF_MUSIC_ASSISTANT_URL] = "invalid_url"
            if music_assistant_external_url and not music_assistant_external_url.startswith("https://"):
                errors[CONF_MUSIC_ASSISTANT_EXTERNAL_URL] = "invalid_external_url"
            if not music_assistant_url:
                errors[CONF_MUSIC_ASSISTANT_URL] = "required"
            if not effective_token:
                errors[CONF_MUSIC_ASSISTANT_TOKEN] = "required"
            updated = dict(self._config_entry.options)
            updated.update(user_input)
            if effective_token:
                updated[CONF_MUSIC_ASSISTANT_TOKEN] = effective_token
            if not errors:
                connection_error = await _validate_music_assistant_api(
                    self.hass, music_assistant_url, effective_token
                )
                if connection_error:
                    errors["base"] = connection_error
            if not errors:
                return self.async_create_entry(title="", data=updated)

        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PROFILE_ID,
                        default=self._config_entry.options.get(
                            CONF_PROFILE_ID,
                            self._config_entry.data.get(CONF_PROFILE_ID, DEFAULT_PROFILE_ID),
                        ),
                    ): str,
                    vol.Optional(
                        CONF_ENABLE_EXPERIMENTAL,
                        default=self._config_entry.options.get(CONF_ENABLE_EXPERIMENTAL, False),
                    ): bool,
                    vol.Required(
                        CONF_MUSIC_ASSISTANT_URL,
                        default=self._config_entry.options.get(
                            CONF_MUSIC_ASSISTANT_URL,
                            self._config_entry.data.get(CONF_MUSIC_ASSISTANT_URL, ""),
                        ),
                    ): str,
                    vol.Optional(
                        CONF_MUSIC_ASSISTANT_EXTERNAL_URL,
                        default=self._config_entry.options.get(
                            CONF_MUSIC_ASSISTANT_EXTERNAL_URL,
                            self._config_entry.data.get(CONF_MUSIC_ASSISTANT_EXTERNAL_URL, ""),
                        ),
                    ): str,
                    vol.Optional(CONF_MUSIC_ASSISTANT_TOKEN, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_add_volume_rule(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Add or update a volume rule."""
        runtime = _runtime_from_hass(self.hass)
        players = _player_choices(runtime)
        errors: dict[str, str] = {}
        if user_input is not None:
            if runtime is None:
                errors["base"] = "runtime_unavailable"
            elif not players or not user_input.get("player"):
                errors["base"] = "no_music_assistant_players"
            else:
                try:
                    await runtime.async_set_volume_rule(
                        {
                            CONF_PROFILE_ID: _profile_id(self._config_entry),
                            "player": user_input["player"],
                            "max_volume": user_input["max_volume"],
                            "start_time": user_input.get("start_time", ""),
                            "end_time": user_input.get("end_time", ""),
                            "days": _parse_days(user_input.get("days")),
                            "enabled": user_input.get("enabled", True),
                        }
                    )
                    return await self.async_step_init()
                except ValueError:
                    errors["base"] = "invalid_days"

        return self.async_show_form(
            step_id="add_volume_rule",
            data_schema=vol.Schema(
                {
                    vol.Required("player"): vol.In(players or {"": "No Music Assistant players detected"}),
                    vol.Required("max_volume", default=50): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Optional("start_time", default=""): str,
                    vol.Optional("end_time", default=""): str,
                    vol.Optional("days", default=""): str,
                    vol.Optional("enabled", default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_delete_volume_rule(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Delete one volume rule."""
        runtime = _runtime_from_hass(self.hass)
        choices = _volume_rule_choices(runtime, _profile_id(self._config_entry))
        errors: dict[str, str] = {}
        if user_input is not None:
            if runtime is None:
                errors["base"] = "runtime_unavailable"
            elif not choices or not user_input.get("player"):
                errors["base"] = "no_volume_rules"
            else:
                await runtime.async_delete_volume_rule(
                    {CONF_PROFILE_ID: _profile_id(self._config_entry), "player": user_input["player"]}
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id="delete_volume_rule",
            data_schema=vol.Schema(
                {
                    vol.Required("player"): vol.In(choices or {"": "No volume rules configured"}),
                }
            ),
            errors=errors,
        )

    async def async_step_add_schedule(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Add or update a schedule."""
        runtime = _runtime_from_hass(self.hass)
        players = _player_choices(runtime)
        errors: dict[str, str] = {}
        if user_input is not None:
            if runtime is None:
                errors["base"] = "runtime_unavailable"
            elif not players or not user_input.get("player"):
                errors["base"] = "no_music_assistant_players"
            else:
                try:
                    await runtime.async_set_schedule(
                        {
                            CONF_PROFILE_ID: _profile_id(self._config_entry),
                            "id": user_input.get("id"),
                            "name": user_input.get("name"),
                            "player": user_input["player"],
                            "media_id": user_input["media_id"],
                            "media_type": user_input["media_type"],
                            "time": user_input["time"],
                            "days": _parse_days(user_input.get("days")),
                            "volume": user_input.get("volume"),
                            "enabled": user_input.get("enabled", True),
                        }
                    )
                    return await self.async_step_init()
                except ValueError:
                    errors["base"] = "invalid_days"

        return self.async_show_form(
            step_id="add_schedule",
            data_schema=vol.Schema(
                {
                    vol.Optional("id", default=""): str,
                    vol.Optional("name", default=""): str,
                    vol.Required("player"): vol.In(players or {"": "No Music Assistant players detected"}),
                    vol.Required("media_id"): str,
                    vol.Required("media_type", default="music"): vol.In(MEDIA_TYPES),
                    vol.Required("time"): str,
                    vol.Optional("days", default=""): str,
                    vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Optional("enabled", default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_delete_schedule(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Delete one schedule."""
        runtime = _runtime_from_hass(self.hass)
        choices = _schedule_choices(runtime, _profile_id(self._config_entry))
        errors: dict[str, str] = {}
        if user_input is not None:
            if runtime is None:
                errors["base"] = "runtime_unavailable"
            elif not choices or not user_input.get("id"):
                errors["base"] = "no_schedules"
            else:
                await runtime.async_delete_schedule(
                    {CONF_PROFILE_ID: _profile_id(self._config_entry), "id": user_input["id"]}
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id="delete_schedule",
            data_schema=vol.Schema(
                {
                    vol.Required("id"): vol.In(choices or {"": "No schedules configured"}),
                }
            ),
            errors=errors,
        )

    async def async_step_run_schedule(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Run one stored schedule immediately."""
        runtime = _runtime_from_hass(self.hass)
        choices = _schedule_choices(runtime, _profile_id(self._config_entry))
        errors: dict[str, str] = {}
        if user_input is not None:
            if runtime is None:
                errors["base"] = "runtime_unavailable"
            elif not choices or not user_input.get("id"):
                errors["base"] = "no_schedules"
            else:
                result = await runtime.async_run_schedule_now(
                    {CONF_PROFILE_ID: _profile_id(self._config_entry), "id": user_input["id"]}
                )
                if not result.get("ok"):
                    errors["base"] = "schedule_run_failed"
                else:
                    return await self.async_step_init()

        return self.async_show_form(
            step_id="run_schedule",
            data_schema=vol.Schema(
                {
                    vol.Required("id"): vol.In(choices or {"": "No schedules configured"}),
                }
            ),
            errors=errors,
        )

    async def async_step_add_timer(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Add or update a one-shot timer."""
        runtime = _runtime_from_hass(self.hass)
        players = _player_choices(runtime)
        errors: dict[str, str] = {}
        if user_input is not None:
            if runtime is None:
                errors["base"] = "runtime_unavailable"
            elif not players or not user_input.get("player"):
                errors["base"] = "no_music_assistant_players"
            else:
                await runtime.async_set_timer(
                    {
                        CONF_PROFILE_ID: _profile_id(self._config_entry),
                        "id": user_input.get("id"),
                        "player": user_input["player"],
                        "action": user_input["action"],
                        "minutes": user_input["minutes"],
                        "enabled": user_input.get("enabled", True),
                        "origin": "config_flow",
                    }
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id="add_timer",
            data_schema=vol.Schema(
                {
                    vol.Optional("id", default=""): str,
                    vol.Required("player"): vol.In(players or {"": "No Music Assistant players detected"}),
                    vol.Required("action", default="stop"): vol.In({"stop": "Stop", "pause": "Pause"}),
                    vol.Required("minutes", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                    vol.Optional("enabled", default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_delete_timer(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Delete one timer."""
        runtime = _runtime_from_hass(self.hass)
        choices = _timer_choices(runtime, _profile_id(self._config_entry))
        errors: dict[str, str] = {}
        if user_input is not None:
            if runtime is None:
                errors["base"] = "runtime_unavailable"
            elif not choices or not user_input.get("id"):
                errors["base"] = "no_timers"
            else:
                await runtime.async_delete_timer(
                    {CONF_PROFILE_ID: _profile_id(self._config_entry), "id": user_input["id"]}
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id="delete_timer",
            data_schema=vol.Schema(
                {
                    vol.Required("id"): vol.In(choices or {"": "No timers configured"}),
                }
            ),
            errors=errors,
        )

    async def async_step_run_orchestration(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Run one orchestration pass."""
        runtime = _runtime_from_hass(self.hass)
        if runtime is not None:
            await runtime.async_tick_orchestration()
        return await self.async_step_init()

    async def async_step_done(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Close the options flow."""
        return self.async_create_entry(title="", data=dict(self._config_entry.options))
