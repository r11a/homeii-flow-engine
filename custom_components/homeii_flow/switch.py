"""Switch entities for HOMEii Flow Engine schedules, timers and volume rules."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time

from . import async_get_runtime
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, DEFAULT_PROFILE_ID, DOMAIN, NAME, SIGNAL_ENGINE_UPDATED, VERSION
from .runtime import (
    HomeiiFlowRuntime,
    _due_schedule_datetime,
    _homeii_weekday,
    _local_datetime,
    _next_schedule_datetime,
    _parse_utc_datetime,
    _utc_iso,
)


def _profile_id(entry: ConfigEntry) -> str:
    """Return the active profile id for a config entry."""
    return str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)


def _schedule_key(schedule: dict[str, Any], profile_id: str) -> str:
    """Return a stable key for one schedule."""
    return f"{profile_id}:{schedule.get('id') or schedule.get('schedule_id') or ''}"


def _timer_key(timer: dict[str, Any], profile_id: str) -> str:
    """Return a stable key for one timer."""
    return f"{profile_id}:{timer.get('id') or timer.get('timer_id') or ''}"


def _volume_rule_key(rule: dict[str, Any], profile_id: str) -> str:
    """Return a stable key for one volume rule."""
    return f"{profile_id}:{rule.get('player') or rule.get('entity_id') or ''}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HOMEii Flow Engine schedule, timer and volume-rule switches."""
    runtime = async_get_runtime(hass)
    profile_id = _profile_id(entry)
    known_schedules: dict[str, HomeiiFlowScheduleSwitch] = {}
    known_timers: dict[str, HomeiiFlowTimerSwitch] = {}
    known_volume_rules: dict[str, HomeiiFlowVolumeRuleSwitch] = {}
    async_add_entities([HomeiiFlowSystemScreensaverSwitch(runtime, entry, profile_id)])

    @callback
    def add_missing_items() -> None:
        current_schedule_keys: set[str] = set()
        current_timer_keys: set[str] = set()
        current_volume_rule_keys: set[str] = set()
        entities: list[SwitchEntity] = []
        for schedule in runtime.schedules(profile_id):
            schedule_id = str(schedule.get("id") or schedule.get("schedule_id") or "").strip()
            if not schedule_id:
                continue
            key = _schedule_key(schedule, profile_id)
            current_schedule_keys.add(key)
            if key in known_schedules:
                continue
            entity = HomeiiFlowScheduleSwitch(runtime, entry, profile_id, schedule_id)
            known_schedules[key] = entity
            entities.append(entity)
        for stale_key in [key for key in known_schedules if key not in current_schedule_keys]:
            known_schedules.pop(stale_key).remove_from_registry()
        for timer in runtime.timers(profile_id):
            timer_id = str(timer.get("id") or timer.get("timer_id") or "").strip()
            if not timer_id:
                continue
            key = _timer_key(timer, profile_id)
            current_timer_keys.add(key)
            if key in known_timers:
                continue
            entity = HomeiiFlowTimerSwitch(runtime, entry, profile_id, timer_id)
            known_timers[key] = entity
            entities.append(entity)
        for stale_key in [key for key in known_timers if key not in current_timer_keys]:
            known_timers.pop(stale_key).remove_from_registry()
        for rule in runtime.volume_rules(profile_id):
            player = str(rule.get("player") or rule.get("entity_id") or "").strip()
            if not player:
                continue
            key = _volume_rule_key(rule, profile_id)
            current_volume_rule_keys.add(key)
            if key in known_volume_rules:
                continue
            entity = HomeiiFlowVolumeRuleSwitch(runtime, entry, profile_id, player)
            known_volume_rules[key] = entity
            entities.append(entity)
        for stale_key in [key for key in known_volume_rules if key not in current_volume_rule_keys]:
            known_volume_rules.pop(stale_key).remove_from_registry()
        _remove_stale_registry_entries(hass, entry, profile_id, "schedule", current_schedule_keys, _schedule_key)
        _remove_stale_registry_entries(hass, entry, profile_id, "timer", current_timer_keys, _timer_key)
        _remove_stale_registry_entries(
            hass,
            entry,
            profile_id,
            "volume_rule",
            current_volume_rule_keys,
            _volume_rule_key,
        )
        if entities:
            async_add_entities(entities)

    add_missing_items()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ENGINE_UPDATED, add_missing_items))


def _remove_stale_registry_entries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    profile_id: str,
    kind: str,
    current_keys: set[str],
    key_fn: Callable[[dict[str, Any], str], str],
) -> None:
    """Remove switch registry entries whose backing storage item no longer exists."""
    registry = async_get_entity_registry(hass)
    prefix = f"{entry.entry_id}_{kind}_"
    for registry_entry in list(registry.entities.values()):
        if getattr(registry_entry, "config_entry_id", None) != entry.entry_id:
            continue
        unique_id = str(getattr(registry_entry, "unique_id", "") or "")
        if not unique_id.startswith(prefix):
            continue
        item_id = unique_id.removeprefix(prefix)
        key = key_fn({"id": item_id, "schedule_id": item_id, "timer_id": item_id, "player": item_id, "entity_id": item_id}, profile_id)
        if key in current_keys:
            continue
        registry.async_remove(registry_entry.entity_id)


def _async_remove_entity(entity: SwitchEntity) -> None:
    """Remove a live entity from its platform when Home Assistant exposes that hook."""
    hass = getattr(entity, "hass", None)
    if hass is None:
        return
    remove_fn = getattr(entity, "async_remove", None)
    if not callable(remove_fn):
        return
    remove_result = remove_fn(force_remove=True)
    if hasattr(remove_result, "__await__"):
        hass.async_create_task(remove_result)


class HomeiiFlowSystemScreensaverSwitch(SwitchEntity):
    """Enable or disable the system-wide HOMEii screensaver agent."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:monitor-screenshot"

    def __init__(self, runtime: HomeiiFlowRuntime, entry: ConfigEntry, profile_id: str) -> None:
        """Initialize the system screensaver switch."""
        self._runtime = runtime
        self._entry = entry
        self._profile_id = profile_id
        self._attr_unique_id = f"{entry.entry_id}_system_screensaver"
        self._attr_name = "System screensaver"

    async def async_added_to_hass(self) -> None:
        """Subscribe to Engine updates."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self.async_write_ha_state))

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        instance_id = str(self._entry.data.get(CONF_INSTANCE_ID) or "default")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or NAME,
            manufacturer="HOMEii",
            model="Flow Engine",
            sw_version=VERSION,
            configuration_url="https://github.com/r11a/homeii-flow-engine",
            suggested_area=instance_id if instance_id != "default" else None,
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the system screensaver is enabled."""
        return bool(self._runtime.screensaver_config(self._profile_id).get("enabled"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return screensaver configuration details."""
        return self._runtime.screensaver_state(self._profile_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the system screensaver."""
        await self._runtime.async_set_screensaver_config({"profile_id": self._profile_id, "enabled": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the system screensaver."""
        await self._runtime.async_set_screensaver_config({"profile_id": self._profile_id, "enabled": False})


class HomeiiFlowScheduleSwitch(SwitchEntity):
    """A HOMEii schedule represented as an HA switch with its own timer."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        profile_id: str,
        schedule_id: str,
    ) -> None:
        """Initialize the schedule switch."""
        self._runtime = runtime
        self._entry = entry
        self._profile_id = profile_id
        self._schedule_id = schedule_id
        self._timer_unsub: Callable[[], None] | None = None
        self._next_run: datetime | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_triggered_at = ""
        self._removed = False
        self._attr_unique_id = f"{entry.entry_id}_schedule_{schedule_id}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to schedule updates and start the schedule timer."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self._handle_engine_update))
        self._reschedule()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the schedule timer when the switch is removed."""
        self._cancel_timer()

    def remove_from_registry(self) -> None:
        """Remove this switch from Home Assistant's entity registry."""
        self._removed = True
        self._cancel_timer()
        _async_remove_entity(self)
        if not self.entity_id:
            return
        registry = async_get_entity_registry(self.hass)
        if registry.async_get(self.entity_id):
            registry.async_remove(self.entity_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        instance_id = str(self._entry.data.get(CONF_INSTANCE_ID) or "default")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or NAME,
            manufacturer="HOMEii",
            model="Flow Engine",
            sw_version=VERSION,
            configuration_url="https://github.com/r11a/homeii-flow-engine",
            suggested_area=instance_id if instance_id != "default" else None,
        )

    @property
    def name(self) -> str | None:
        """Return the schedule name."""
        schedule = self._schedule()
        return str(schedule.get("name") or self._schedule_id) if schedule else self._schedule_id

    @property
    def is_on(self) -> bool | None:
        """Return whether the schedule is enabled."""
        schedule = self._schedule()
        if schedule is None:
            return False
        return bool(schedule.get("enabled", True))

    @property
    def available(self) -> bool:
        """Return whether the backing schedule still exists."""
        return self._schedule() is not None and not self._removed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return schedule diagnostics."""
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
            "media_mode": schedule.get("media_mode"),
            "playlist": schedule.get("playlist"),
            "playlist_name": schedule.get("playlist_name"),
            "next_run": self._next_run.isoformat() if self._next_run else "",
            "last_triggered_at": self._last_triggered_at,
            "last_result": self._last_result,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the schedule."""
        schedule = self._schedule()
        if schedule is None:
            return
        payload = dict(schedule)
        payload["enabled"] = True
        await self._runtime.async_set_schedule(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the schedule."""
        schedule = self._schedule()
        if schedule is None:
            return
        payload = dict(schedule)
        payload["enabled"] = False
        await self._runtime.async_set_schedule(payload)

    @callback
    def _handle_engine_update(self) -> None:
        """Handle schedule storage updates."""
        if self._schedule() is None:
            self.remove_from_registry()
            return
        self._reschedule()
        self.async_write_ha_state()

    def _schedule(self) -> dict[str, Any] | None:
        """Return the backing schedule."""
        for schedule in self._runtime.schedules(self._profile_id):
            if str(schedule.get("id") or schedule.get("schedule_id") or "").strip() == self._schedule_id:
                return schedule
        return None

    def _cancel_timer(self) -> None:
        """Cancel the active point-in-time timer."""
        if self._timer_unsub is not None:
            self._timer_unsub()
            self._timer_unsub = None
        self._next_run = None

    def _reschedule(self) -> None:
        """Register the next timer for this schedule."""
        self._cancel_timer()
        schedule = self._schedule()
        if schedule is None or not bool(schedule.get("enabled", True)):
            return

        now = _local_datetime()
        due_at = _due_schedule_datetime(schedule, now)
        if due_at is not None:
            run_key = self._runtime._schedule_run_key(schedule, due_at)
            schedule_key = self._runtime._schedule_storage_key(schedule)
            if self._runtime._last_schedule_runs.get(schedule_key) != run_key:
                self.hass.async_create_task(self._async_fire(due_at, "switch_catchup"))
                return
            now = now + timedelta(seconds=121)

        next_run = _next_schedule_datetime(schedule, now, include_due=False)
        if next_run is None:
            return
        self._next_run = next_run
        run_at = next_run if next_run.tzinfo is not None else next_run.replace(tzinfo=UTC)

        @callback
        def timer_finished(now_value: datetime) -> None:
            self.hass.async_create_task(self._async_fire(_local_datetime(now_value), "switch_timer"))

        self._timer_unsub = async_track_point_in_time(self.hass, timer_finished, run_at.astimezone(UTC))

    async def _async_fire(self, due_at: datetime, trigger: str) -> None:
        """Execute the schedule and reschedule its next timer."""
        schedule = self._schedule()
        if schedule is None:
            self.remove_from_registry()
            return

        run_key = self._runtime._schedule_run_key(schedule, due_at)
        schedule_key = self._runtime._schedule_storage_key(schedule)
        if self._runtime._last_schedule_runs.get(schedule_key) == run_key:
            self._reschedule()
            self.async_write_ha_state()
            return

        self._last_triggered_at = _utc_iso()
        try:
            result = await self._runtime.async_run_scheduled_schedule(
                self._profile_id, self._schedule_id, due_at
            )
        except Exception as err:  # noqa: BLE001 - surfaced in schedule attributes
            result = {
                "ok": False,
                "schedule_id": self._schedule_id,
                "profile_id": self._profile_id,
                "phase": "exception",
                "trigger": trigger,
                "error": str(err),
                "executed_at": _utc_iso(),
            }
        result["trigger"] = trigger
        result["due_at"] = due_at.isoformat()
        result["runner"] = "homeii_schedule_switch"

        self._last_result = result
        self._runtime._last_schedule_runs[schedule_key] = run_key
        self._runtime._last_schedule_action = result
        self._runtime._last_schedule_check = {
            "checked_at": _utc_iso(),
            "local_time": _local_datetime().isoformat(),
            "local_weekday": _homeii_weekday(_local_datetime()),
            "trigger": trigger,
            "schedule_count": len(self._runtime.schedules()),
            "due_schedule_ids": [self._schedule_id],
            "attempted_count": 1,
            "executed_count": 1 if result.get("ok") else 0,
            "failed_count": 0 if result.get("ok") else 1,
            "due_at": due_at.isoformat(),
        }
        if result.get("ok") and str(schedule.get("after_run") or "") == "disable":
            payload = dict(schedule)
            payload["enabled"] = False
            await self._runtime.async_set_schedule(payload)
        self._reschedule()
        self.async_write_ha_state()
        self._runtime.hass.async_create_task(self._runtime.async_tick_orchestration(trigger="schedule_switch"))


class HomeiiFlowTimerSwitch(SwitchEntity):
    """A one-shot HOMEii timer represented as an HA switch with its own timer."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        profile_id: str,
        timer_id: str,
    ) -> None:
        """Initialize the timer switch."""
        self._runtime = runtime
        self._entry = entry
        self._profile_id = profile_id
        self._timer_id = timer_id
        self._timer_unsub: Callable[[], None] | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_triggered_at = ""
        self._removed = False
        self._attr_unique_id = f"{entry.entry_id}_timer_{timer_id}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to timer updates and start the one-shot timer."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self._handle_engine_update))
        self._reschedule()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the one-shot timer when the switch is removed."""
        self._cancel_timer()

    def remove_from_registry(self) -> None:
        """Remove this switch from Home Assistant's entity registry."""
        self._removed = True
        self._cancel_timer()
        _async_remove_entity(self)
        if not self.entity_id:
            return
        registry = async_get_entity_registry(self.hass)
        if registry.async_get(self.entity_id):
            registry.async_remove(self.entity_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        instance_id = str(self._entry.data.get(CONF_INSTANCE_ID) or "default")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or NAME,
            manufacturer="HOMEii",
            model="Flow Engine",
            sw_version=VERSION,
            configuration_url="https://github.com/r11a/homeii-flow-engine",
            suggested_area=instance_id if instance_id != "default" else None,
        )

    @property
    def name(self) -> str | None:
        """Return the timer name."""
        timer = self._timer()
        if not timer:
            return self._timer_id
        player = str(timer.get("player") or "").strip()
        player_state = self.hass.states.get(player) if player else None
        player_attrs = player_state.attributes if player_state is not None else {}
        player_name = str(player_attrs.get("friendly_name") or player or self._timer_id)
        timer_type = str(timer.get("type") or timer.get("timer_type") or "Timer").replace("_", " ").title()
        return f"{timer_type}: {player_name}"

    @property
    def is_on(self) -> bool | None:
        """Return whether the timer is enabled."""
        timer = self._timer()
        if timer is None:
            return False
        return bool(timer.get("enabled", True))

    @property
    def available(self) -> bool:
        """Return whether the backing timer still exists."""
        return self._timer() is not None and not self._removed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return timer diagnostics."""
        timer = self._timer() or {}
        ends_at = _parse_utc_datetime(timer.get("ends_at") or timer.get("target_at"))
        remaining = int((ends_at - datetime.now(UTC)).total_seconds()) if ends_at else 0
        return {
            "profile_id": self._profile_id,
            "timer_id": self._timer_id,
            "type": timer.get("type") or timer.get("timer_type"),
            "player": timer.get("player"),
            "action": timer.get("action"),
            "minutes": timer.get("minutes"),
            "ends_at": ends_at.isoformat() if ends_at else "",
            "remaining_seconds": max(0, remaining),
            "origin": timer.get("origin"),
            "last_triggered_at": self._last_triggered_at,
            "last_result": self._last_result,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the timer."""
        timer = self._timer()
        if timer is None:
            return
        payload = dict(timer)
        payload["enabled"] = True
        await self._runtime.async_set_timer(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the timer without deleting it."""
        timer = self._timer()
        if timer is None:
            return
        payload = dict(timer)
        payload["enabled"] = False
        await self._runtime.async_set_timer(payload)

    @callback
    def _handle_engine_update(self) -> None:
        """Handle timer storage updates."""
        if self._timer() is None:
            self.remove_from_registry()
            return
        self._reschedule()
        self.async_write_ha_state()

    def _timer(self) -> dict[str, Any] | None:
        """Return the backing timer."""
        for timer in self._runtime.timers(self._profile_id):
            if str(timer.get("id") or timer.get("timer_id") or "").strip() == self._timer_id:
                return timer
        return None

    def _cancel_timer(self) -> None:
        """Cancel the active point-in-time timer."""
        if self._timer_unsub is not None:
            self._timer_unsub()
            self._timer_unsub = None

    def _reschedule(self) -> None:
        """Register the one-shot timer."""
        self._cancel_timer()
        timer = self._timer()
        if timer is None or not bool(timer.get("enabled", True)):
            return
        ends_at = _parse_utc_datetime(timer.get("ends_at") or timer.get("target_at"))
        if ends_at is None:
            return
        now_utc = datetime.now(UTC)
        if ends_at <= now_utc:
            self.hass.async_create_task(self._async_fire(ends_at, "timer_switch_catchup"))
            return

        @callback
        def timer_finished(now_value: datetime) -> None:
            self.hass.async_create_task(self._async_fire(now_value.astimezone(UTC), "timer_switch"))

        self._timer_unsub = async_track_point_in_time(self.hass, timer_finished, ends_at)

    async def _async_fire(self, due_at: datetime, trigger: str) -> None:
        """Execute and remove the one-shot timer."""
        if self._last_triggered_at:
            return
        timer = self._timer()
        if timer is None:
            self.remove_from_registry()
            return
        self._last_triggered_at = _utc_iso()
        try:
            result = await self._runtime.async_execute_timer(timer)
        except Exception as err:  # noqa: BLE001 - surfaced in timer attributes and sensors
            result = {
                "ok": False,
                "timer_id": self._timer_id,
                "profile_id": self._profile_id,
                "trigger": trigger,
                "due_at": due_at.isoformat(),
                "error": str(err),
                "executed_at": _utc_iso(),
            }
        result["trigger"] = trigger
        result["due_at"] = due_at.isoformat()
        result["runner"] = "homeii_timer_switch"
        self._last_result = result
        self._runtime._last_timer_action = result
        await self._runtime.async_delete_timer({"profile_id": self._profile_id, "timer_id": self._timer_id})
        self.remove_from_registry()


class HomeiiFlowVolumeRuleSwitch(SwitchEntity):
    """A stored HOMEii volume rule represented as a Home Assistant switch."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:volume-vibrate"

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        profile_id: str,
        player: str,
    ) -> None:
        """Initialize the volume-rule switch."""
        self._runtime = runtime
        self._entry = entry
        self._profile_id = profile_id
        self._player = player
        self._removed = False
        self._attr_unique_id = f"{entry.entry_id}_volume_rule_{player}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to volume-rule updates."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self._handle_engine_update))

    def remove_from_registry(self) -> None:
        """Remove this switch from Home Assistant's entity registry."""
        self._removed = True
        _async_remove_entity(self)
        if not self.entity_id:
            return
        registry = async_get_entity_registry(self.hass)
        if registry.async_get(self.entity_id):
            registry.async_remove(self.entity_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        instance_id = str(self._entry.data.get(CONF_INSTANCE_ID) or "default")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or NAME,
            manufacturer="HOMEii",
            model="Flow Engine",
            sw_version=VERSION,
            configuration_url="https://github.com/r11a/homeii-flow-engine",
            suggested_area=instance_id if instance_id != "default" else None,
        )

    @property
    def name(self) -> str | None:
        """Return the volume-rule name."""
        state = self.hass.states.get(self._player)
        player_name = str((state.attributes or {}).get("friendly_name") or self._player) if state else self._player
        return f"Volume limit: {player_name}"

    @property
    def is_on(self) -> bool | None:
        """Return whether the rule is enabled."""
        rule = self._rule()
        if rule is None:
            return False
        return bool(rule.get("enabled", True))

    @property
    def available(self) -> bool:
        """Return whether the backing rule still exists."""
        return self._rule() is not None and not self._removed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return volume-rule diagnostics."""
        rule = self._rule() or {}
        state = self.hass.states.get(self._player)
        current_volume = None
        if state is not None and isinstance(state.attributes.get("volume_level"), (int, float)):
            current_volume = round(float(state.attributes["volume_level"]) * 100)
        return {
            "profile_id": self._profile_id,
            "player": self._player,
            "enabled": bool(rule.get("enabled", True)),
            "max_volume": rule.get("max_volume"),
            "current_volume": current_volume,
            "start_time": rule.get("start_time"),
            "end_time": rule.get("end_time"),
            "days": rule.get("days"),
            "active_now": self._runtime._volume_rule_active(rule, _local_datetime()) if rule else False,
            "last_volume_action": self._runtime.orchestration_status().get("last_volume_action"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable this volume rule."""
        rule = self._rule()
        if rule is None:
            return
        payload = dict(rule)
        payload["enabled"] = True
        await self._runtime.async_set_volume_rule(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable this volume rule."""
        rule = self._rule()
        if rule is None:
            return
        payload = dict(rule)
        payload["enabled"] = False
        await self._runtime.async_set_volume_rule(payload)

    @callback
    def _handle_engine_update(self) -> None:
        """Handle volume-rule storage updates."""
        if self._rule() is None:
            self.remove_from_registry()
            return
        self.async_write_ha_state()

    def _rule(self) -> dict[str, Any] | None:
        """Return the backing volume rule."""
        for rule in self._runtime.volume_rules(self._profile_id):
            if str(rule.get("player") or rule.get("entity_id") or "").strip() == self._player:
                return rule
        return None
