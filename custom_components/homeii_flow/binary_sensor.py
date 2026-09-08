"""Binary sensors for HOMEii Flow Engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import async_get_runtime
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, DEFAULT_INSTANCE_ID, DEFAULT_PROFILE_ID, DOMAIN, NAME, SIGNAL_ENGINE_UPDATED, VERSION
from .runtime import HomeiiFlowRuntime


@dataclass(frozen=True, kw_only=True)
class HomeiiFlowBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a HOMEii Flow Engine binary sensor."""

    is_on_fn: Callable[[HomeiiFlowRuntime, ConfigEntry], bool]
    attrs_fn: Callable[[HomeiiFlowRuntime, ConfigEntry], dict[str, Any]] | None = None


def _profile_id(entry: ConfigEntry) -> str:
    """Return the active profile id for a config entry."""
    return str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)


def _connection_ok(runtime: HomeiiFlowRuntime, key: str) -> bool:
    """Return whether a required connection is healthy."""
    snapshot = runtime.required_connections_snapshot()
    value = snapshot.get(key)
    return bool(isinstance(value, dict) and value.get("ok"))


def _connection_attrs(runtime: HomeiiFlowRuntime, key: str) -> dict[str, Any]:
    """Return one required-connection attributes mapping."""
    snapshot = runtime.required_connections_snapshot()
    value = snapshot.get(key)
    return value if isinstance(value, dict) else {}


BINARY_SENSORS: tuple[HomeiiFlowBinarySensorDescription, ...] = (
    HomeiiFlowBinarySensorDescription(
        key="required_connections_ok",
        name="Required connections OK",
        icon="mdi:connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda runtime, entry: bool(runtime.required_connections_snapshot().get("ok")),
        attrs_fn=lambda runtime, entry: runtime.required_connections_snapshot(),
    ),
    HomeiiFlowBinarySensorDescription(
        key="music_assistant_connected",
        name="Music Assistant connected",
        icon="mdi:music-circle",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda runtime, entry: _connection_ok(runtime, "music_assistant"),
        attrs_fn=lambda runtime, entry: _connection_attrs(runtime, "music_assistant"),
    ),
    HomeiiFlowBinarySensorDescription(
        key="queue_provider_connected",
        name="Queue provider connected",
        icon="mdi:playlist-music",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda runtime, entry: _connection_ok(runtime, "queue_provider"),
        attrs_fn=lambda runtime, entry: _connection_attrs(runtime, "queue_provider"),
    ),
    HomeiiFlowBinarySensorDescription(
        key="library_provider_connected",
        name="Library provider connected",
        icon="mdi:library",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda runtime, entry: _connection_ok(runtime, "library_provider"),
        attrs_fn=lambda runtime, entry: _connection_attrs(runtime, "library_provider"),
    ),
    HomeiiFlowBinarySensorDescription(
        key="search_provider_connected",
        name="Search provider connected",
        icon="mdi:magnify",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda runtime, entry: _connection_ok(runtime, "search_provider"),
        attrs_fn=lambda runtime, entry: _connection_attrs(runtime, "search_provider"),
    ),
    HomeiiFlowBinarySensorDescription(
        key="any_player_playing",
        name="Any player playing",
        icon="mdi:play-network",
        is_on_fn=lambda runtime, entry: int(runtime.cached_stats().get("players_playing", 0)) > 0,
        attrs_fn=lambda runtime, entry: {
            "playing_entities": runtime.cached_stats().get("playing_entities", []),
        },
    ),
    HomeiiFlowBinarySensorDescription(
        key="group_active",
        name="Group active",
        icon="mdi:speaker-multiple",
        is_on_fn=lambda runtime, entry: int(runtime.cached_stats().get("players_grouped", 0)) > 0,
        attrs_fn=lambda runtime, entry: {
            "grouped_entities": runtime.cached_stats().get("grouped_entities", []),
            "active_queue_groups": runtime.cached_stats().get("active_queue_groups", {}),
        },
    ),
    HomeiiFlowBinarySensorDescription(
        key="volume_policy_active",
        name="Volume policy active",
        icon="mdi:volume-lock",
        is_on_fn=lambda runtime, entry: len(runtime.active_volume_rule_summaries(_profile_id(entry))) > 0,
        attrs_fn=lambda runtime, entry: {
            "active_volume_rules": runtime.active_volume_rule_summaries(_profile_id(entry)),
            "volume_rules": runtime.volume_rule_summaries(_profile_id(entry)),
            "last_volume_action": runtime.orchestration_status().get("last_volume_action"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HOMEii Flow Engine binary sensors."""
    runtime = async_get_runtime(hass)
    async_add_entities(HomeiiFlowBinarySensor(runtime, entry, description) for description in BINARY_SENSORS)


class HomeiiFlowBinarySensor(BinarySensorEntity):
    """HOMEii Flow Engine binary sensor."""

    entity_description: HomeiiFlowBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        description: HomeiiFlowBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        self._runtime = runtime
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_name = description.name

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
    def is_on(self) -> bool:
        """Return whether the binary sensor is on."""
        return bool(self.entity_description.is_on_fn(self._runtime, self._entry))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return sensor attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._runtime, self._entry)
