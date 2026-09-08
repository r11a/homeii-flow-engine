"""Button entities for HOMEii Flow Engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import async_get_runtime
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, DEFAULT_INSTANCE_ID, DEFAULT_PROFILE_ID, DOMAIN, NAME, SIGNAL_ENGINE_UPDATED, VERSION
from .runtime import HomeiiFlowRuntime, _utc_iso


@dataclass(frozen=True, kw_only=True)
class HomeiiFlowButtonDescription(ButtonEntityDescription):
    """Describe a HOMEii Flow Engine button."""

    action: str


BUTTONS: tuple[HomeiiFlowButtonDescription, ...] = (
    HomeiiFlowButtonDescription(
        key="refresh_state",
        name="Refresh Engine state",
        icon="mdi:refresh",
        action="refresh_state",
    ),
    HomeiiFlowButtonDescription(
        key="run_orchestration",
        name="Run orchestration now",
        icon="mdi:engine-outline",
        action="run_orchestration",
    ),
    HomeiiFlowButtonDescription(
        key="apply_volume_rules",
        name="Apply volume rules now",
        icon="mdi:volume-lock",
        action="apply_volume_rules",
    ),
    HomeiiFlowButtonDescription(
        key="run_next_schedule",
        name="Run next schedule now",
        icon="mdi:calendar-play",
        action="run_next_schedule",
    ),
    HomeiiFlowButtonDescription(
        key="show_system_screensaver_now",
        name="Show system screensaver now",
        icon="mdi:monitor-screenshot",
        action="show_system_screensaver_now",
    ),
)


def _profile_id(entry: ConfigEntry) -> str:
    """Return the active profile id for a config entry."""
    return str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)


def _schedule_key(schedule: dict[str, Any], profile_id: str) -> str:
    """Return a stable key for one schedule."""
    return f"{profile_id}:{schedule.get('id') or schedule.get('schedule_id') or ''}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HOMEii Flow Engine buttons."""
    runtime = async_get_runtime(hass)
    async_add_entities(HomeiiFlowButton(runtime, entry, description) for description in BUTTONS)
    profile_id = _profile_id(entry)
    known_schedule_buttons: dict[str, HomeiiFlowScheduleRunButton] = {}

    @callback
    def add_missing_schedule_buttons() -> None:
        current_keys: set[str] = set()
        entities: list[ButtonEntity] = []
        for schedule in runtime.schedules(profile_id):
            schedule_id = str(schedule.get("id") or schedule.get("schedule_id") or "").strip()
            if not schedule_id:
                continue
            key = _schedule_key(schedule, profile_id)
            current_keys.add(key)
            if key in known_schedule_buttons:
                continue
            entity = HomeiiFlowScheduleRunButton(runtime, entry, profile_id, schedule_id)
            known_schedule_buttons[key] = entity
            entities.append(entity)
        for stale_key in [key for key in known_schedule_buttons if key not in current_keys]:
            known_schedule_buttons.pop(stale_key).remove_from_registry()
        _remove_stale_schedule_buttons(hass, entry, profile_id, current_keys)
        if entities:
            async_add_entities(entities)

    add_missing_schedule_buttons()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ENGINE_UPDATED, add_missing_schedule_buttons))


def _remove_stale_schedule_buttons(
    hass: HomeAssistant,
    entry: ConfigEntry,
    profile_id: str,
    current_keys: set[str],
) -> None:
    """Remove run-now button registry entries whose schedule no longer exists."""
    registry = async_get_entity_registry(hass)
    prefix = f"{entry.entry_id}_run_schedule_"
    for registry_entry in list(registry.entities.values()):
        if getattr(registry_entry, "config_entry_id", None) != entry.entry_id:
            continue
        unique_id = str(getattr(registry_entry, "unique_id", "") or "")
        if not unique_id.startswith(prefix):
            continue
        schedule_id = unique_id.removeprefix(prefix)
        if _schedule_key({"id": schedule_id}, profile_id) in current_keys:
            continue
        registry.async_remove(registry_entry.entity_id)


def _async_remove_button_entity(entity: ButtonEntity) -> None:
    """Remove a live button entity from its platform when Home Assistant supports it."""
    hass = getattr(entity, "hass", None)
    if hass is None:
        return
    remove_fn: Callable[..., Any] | None = getattr(entity, "async_remove", None)
    if not callable(remove_fn):
        return
    remove_result = remove_fn(force_remove=True)
    if hasattr(remove_result, "__await__"):
        hass.async_create_task(remove_result)


class HomeiiFlowButton(ButtonEntity):
    """HOMEii Flow Engine button."""

    entity_description: HomeiiFlowButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        description: HomeiiFlowButtonDescription,
    ) -> None:
        """Initialize the button."""
        self._runtime = runtime
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_name = description.name
        self._last_result: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self.async_write_ha_state)
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        instance_id = str(self._entry.data.get(CONF_INSTANCE_ID) or DEFAULT_INSTANCE_ID)
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or NAME,
            manufacturer="HOMEii",
            model="Flow Engine",
            sw_version=VERSION,
            configuration_url="https://github.com/r11a/homeii-flow-engine",
            suggested_area=instance_id if instance_id != DEFAULT_INSTANCE_ID else None,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return button diagnostics."""
        return {
            "profile_id": _profile_id(self._entry),
            "action": self.entity_description.action,
            "last_result": self._last_result,
            "last_button_action": self._runtime.orchestration_status().get("last_button_action"),
        }

    async def async_press(self) -> None:
        """Run the selected Engine action."""
        action = self.entity_description.action
        result: dict[str, Any]
        if action == "refresh_state":
            self._runtime._stats_cache = None
            result = {"ok": True, "action": action, "executed_at": _utc_iso()}
        elif action == "run_orchestration":
            result = await self._runtime.async_tick_orchestration(trigger="button")
            result["ok"] = True
            result["action"] = action
        elif action == "apply_volume_rules":
            results = await self._runtime.async_enforce_volume_rules()
            result = {
                "ok": True,
                "action": action,
                "applied_count": len(results),
                "results": results,
                "executed_at": _utc_iso(),
            }
        elif action == "run_next_schedule":
            next_schedule = self._runtime.next_schedule_summary(_profile_id(self._entry))
            schedule_id = str(next_schedule.get("id") or "").strip()
            if not schedule_id:
                result = {
                    "ok": False,
                    "action": action,
                    "error": "No enabled schedule is available.",
                    "executed_at": _utc_iso(),
                }
            else:
                result = await self._runtime.async_run_schedule_now(
                    {CONF_PROFILE_ID: _profile_id(self._entry), "id": schedule_id}
                )
                result["action"] = action
        elif action == "show_system_screensaver_now":
            result = await self._runtime.async_request_screensaver_show(
                {CONF_PROFILE_ID: _profile_id(self._entry), "source": "button"}
            )
            result["ok"] = True
            result["action"] = action
        else:
            result = {
                "ok": False,
                "action": action,
                "error": "Unsupported Engine button action.",
                "executed_at": _utc_iso(),
            }
        self._last_result = result
        self._runtime._last_button_action = result
        self._runtime._stats_cache = None
        await self._runtime.async_record_activity(
            "button_pressed",
            f"Button pressed: {action}",
            profile_id=_profile_id(self._entry),
            data=result,
        )
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)


class HomeiiFlowScheduleRunButton(ButtonEntity):
    """Run a specific stored schedule from Home Assistant."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-play"

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        profile_id: str,
        schedule_id: str,
    ) -> None:
        """Initialize the schedule run button."""
        self._runtime = runtime
        self._entry = entry
        self._profile_id = profile_id
        self._schedule_id = schedule_id
        self._removed = False
        self._last_result: dict[str, Any] | None = None
        self._attr_unique_id = f"{entry.entry_id}_run_schedule_{schedule_id}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self._handle_engine_update))

    def remove_from_registry(self) -> None:
        """Remove this button from Home Assistant's entity registry."""
        self._removed = True
        _async_remove_button_entity(self)
        if not self.entity_id:
            return
        registry = async_get_entity_registry(self.hass)
        if registry.async_get(self.entity_id):
            registry.async_remove(self.entity_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        instance_id = str(self._entry.data.get(CONF_INSTANCE_ID) or DEFAULT_INSTANCE_ID)
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or NAME,
            manufacturer="HOMEii",
            model="Flow Engine",
            sw_version=VERSION,
            configuration_url="https://github.com/r11a/homeii-flow-engine",
            suggested_area=instance_id if instance_id != DEFAULT_INSTANCE_ID else None,
        )

    @property
    def name(self) -> str | None:
        """Return the button name."""
        schedule = self._schedule()
        return f"Run now: {schedule.get('name') or self._schedule_id}" if schedule else f"Run now: {self._schedule_id}"

    @property
    def available(self) -> bool:
        """Return whether the backing schedule still exists."""
        return self._schedule() is not None and not self._removed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return schedule-run diagnostics."""
        schedule = self._schedule() or {}
        return {
            "profile_id": self._profile_id,
            "schedule_id": self._schedule_id,
            "time": schedule.get("time"),
            "days": schedule.get("days"),
            "player": schedule.get("player"),
            "media_id": schedule.get("media_id"),
            "media_type": schedule.get("media_type"),
            "media_name": schedule.get("media_name"),
            "last_result": self._last_result,
        }

    async def async_press(self) -> None:
        """Run this schedule immediately."""
        schedule = self._schedule()
        if schedule is None:
            self.remove_from_registry()
            return
        result = await self._runtime.async_run_schedule_now(
            {CONF_PROFILE_ID: self._profile_id, "schedule_id": self._schedule_id}
        )
        result["action"] = "run_schedule_button"
        self._last_result = result
        self._runtime._last_button_action = result
        await self._runtime.async_record_activity(
            "schedule_run_button",
            f"Run now pressed: {schedule.get('name') or self._schedule_id}",
            profile_id=self._profile_id,
            data=result,
        )
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)

    @callback
    def _handle_engine_update(self) -> None:
        """Handle schedule storage updates."""
        if self._schedule() is None:
            self.remove_from_registry()
            return
        self.async_write_ha_state()

    def _schedule(self) -> dict[str, Any] | None:
        """Return the backing schedule."""
        for schedule in self._runtime.schedules(self._profile_id):
            if str(schedule.get("id") or schedule.get("schedule_id") or "").strip() == self._schedule_id:
                return schedule
        return None
