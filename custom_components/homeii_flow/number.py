"""Number entities for HOMEii Flow Engine."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import async_get_runtime
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, DEFAULT_INSTANCE_ID, DEFAULT_PROFILE_ID, DOMAIN, NAME, SIGNAL_ENGINE_UPDATED, VERSION
from .runtime import HomeiiFlowRuntime


def _profile_id(entry: ConfigEntry) -> str:
    """Return the active profile id for a config entry."""
    return str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)


def _volume_rule_key(rule: dict[str, Any], profile_id: str) -> str:
    """Return a stable key for one volume rule."""
    return f"{profile_id}:{rule.get('player') or rule.get('entity_id') or ''}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HOMEii Flow Engine number entities."""
    runtime = async_get_runtime(hass)
    profile_id = _profile_id(entry)
    known_volume_rules: dict[str, HomeiiFlowVolumeRuleNumber] = {}
    async_add_entities([HomeiiFlowScreensaverTimeoutNumber(runtime, entry, profile_id)])

    @callback
    def add_missing_items() -> None:
        current_keys: set[str] = set()
        entities: list[NumberEntity] = []
        for rule in runtime.volume_rules(profile_id):
            player = str(rule.get("player") or rule.get("entity_id") or "").strip()
            if not player:
                continue
            key = _volume_rule_key(rule, profile_id)
            current_keys.add(key)
            if key in known_volume_rules:
                continue
            entity = HomeiiFlowVolumeRuleNumber(runtime, entry, profile_id, player)
            known_volume_rules[key] = entity
            entities.append(entity)
        for stale_key in [key for key in known_volume_rules if key not in current_keys]:
            known_volume_rules.pop(stale_key).remove_from_registry()
        _remove_stale_registry_entries(hass, entry, profile_id, current_keys)
        if entities:
            async_add_entities(entities)

    add_missing_items()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ENGINE_UPDATED, add_missing_items))


def _remove_stale_registry_entries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    profile_id: str,
    current_keys: set[str],
) -> None:
    """Remove number registry entries whose backing volume rule no longer exists."""
    registry = async_get_entity_registry(hass)
    prefix = f"{entry.entry_id}_volume_rule_max_"
    for registry_entry in list(registry.entities.values()):
        if getattr(registry_entry, "config_entry_id", None) != entry.entry_id:
            continue
        unique_id = str(getattr(registry_entry, "unique_id", "") or "")
        if not unique_id.startswith(prefix):
            continue
        player = unique_id.removeprefix(prefix)
        if _volume_rule_key({"player": player}, profile_id) in current_keys:
            continue
        registry.async_remove(registry_entry.entity_id)


def _async_remove_entity(entity: NumberEntity) -> None:
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


class HomeiiFlowVolumeRuleNumber(NumberEntity):
    """A volume rule maximum as a Home Assistant number entity."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        profile_id: str,
        player: str,
    ) -> None:
        """Initialize the volume-rule number."""
        self._runtime = runtime
        self._entry = entry
        self._profile_id = profile_id
        self._player = player
        self._removed = False
        self._attr_unique_id = f"{entry.entry_id}_volume_rule_max_{player}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to Engine updates."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self._handle_engine_update))

    def remove_from_registry(self) -> None:
        """Remove this number from Home Assistant's entity registry."""
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
        """Return the number name."""
        state = self.hass.states.get(self._player)
        player_name = str((state.attributes or {}).get("friendly_name") or self._player) if state else self._player
        return f"Max volume: {player_name}"

    @property
    def available(self) -> bool:
        """Return whether the backing rule still exists."""
        return self._rule() is not None and not self._removed

    @property
    def native_value(self) -> float | None:
        """Return the max-volume value."""
        rule = self._rule()
        if rule is None:
            return None
        try:
            return float(rule.get("max_volume"))
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return number diagnostics."""
        rule = self._rule() or {}
        return {
            "profile_id": self._profile_id,
            "player": self._player,
            "enabled": bool(rule.get("enabled", True)),
            "start_time": rule.get("start_time"),
            "end_time": rule.get("end_time"),
            "days": rule.get("days"),
        }

    async def async_set_native_value(self, value: float) -> None:
        """Update the backing volume rule maximum."""
        rule = self._rule()
        if rule is None:
            return
        payload = dict(rule)
        payload["max_volume"] = int(max(0, min(100, round(float(value)))))
        await self._runtime.async_set_volume_rule(payload)

    @callback
    def _handle_engine_update(self) -> None:
        """Handle Engine updates."""
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


class HomeiiFlowScreensaverTimeoutNumber(NumberEntity):
    """Control the system-wide screensaver idle timeout."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = 15
    _attr_native_max_value = 3600
    _attr_native_step = 15
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime: HomeiiFlowRuntime, entry: ConfigEntry, profile_id: str) -> None:
        """Initialize the screensaver timeout number."""
        self._runtime = runtime
        self._entry = entry
        self._profile_id = profile_id
        self._attr_unique_id = f"{entry.entry_id}_system_screensaver_timeout"
        self._attr_name = "System screensaver timeout"

    async def async_added_to_hass(self) -> None:
        """Subscribe to Engine updates."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self.async_write_ha_state))

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
    def native_value(self) -> float | None:
        """Return the current idle timeout in seconds."""
        return float(self._runtime.screensaver_config(self._profile_id).get("timeout_seconds") or 90)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return screensaver timeout diagnostics."""
        state = self._runtime.screensaver_state(self._profile_id)
        return {
            "profile_id": self._profile_id,
            "enabled": state.get("enabled"),
            "effective_mode": state.get("effective_mode"),
            "show_request_id": state.get("config", {}).get("show_request_id"),
            "show_requested_at": state.get("config", {}).get("show_requested_at"),
        }

    async def async_set_native_value(self, value: float) -> None:
        """Update the system screensaver idle timeout."""
        seconds = int(max(15, min(3600, round(float(value)))))
        await self._runtime.async_set_screensaver_config(
            {CONF_PROFILE_ID: self._profile_id, "timeout_seconds": seconds}
        )
