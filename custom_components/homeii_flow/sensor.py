"""Sensors for HOMEii Flow Engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import async_get_runtime
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, DOMAIN, NAME, SIGNAL_ENGINE_UPDATED, VERSION
from .runtime import HomeiiFlowRuntime


@dataclass(frozen=True, kw_only=True)
class HomeiiFlowSensorDescription(SensorEntityDescription):
    """Describe a HOMEii Flow Engine sensor."""

    value_fn: Callable[[HomeiiFlowRuntime, ConfigEntry], Any]
    attrs_fn: Callable[[HomeiiFlowRuntime, ConfigEntry], dict[str, Any]] | None = None
    force_update: bool = False


def _profile_id(entry: ConfigEntry) -> str:
    """Return the active profile id for a config entry."""
    return str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or "default")


def _connection(runtime: HomeiiFlowRuntime, key: str) -> dict[str, Any]:
    """Return one required-connection snapshot."""
    snapshot = runtime.required_connections_snapshot()
    value = snapshot.get(key)
    return value if isinstance(value, dict) else {}


SENSORS: tuple[HomeiiFlowSensorDescription, ...] = (
    HomeiiFlowSensorDescription(
        key="status",
        name="Status",
        icon="mdi:engine",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda runtime, entry: (
            "connected"
            if runtime.context(instance_id=entry.data.get(CONF_INSTANCE_ID)).get("available")
            else "unknown"
        ),
        attrs_fn=lambda runtime, entry: runtime.context(
            instance_id=entry.data.get(CONF_INSTANCE_ID),
            profile_id=_profile_id(entry),
        ),
    ),
    HomeiiFlowSensorDescription(
        key="required_connections",
        name="Required connections",
        icon="mdi:connection",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda runtime, entry: runtime.required_connections_snapshot().get("status") or "unknown",
        attrs_fn=lambda runtime, entry: runtime.required_connections_snapshot(),
    ),
    HomeiiFlowSensorDescription(
        key="required_connection_music_assistant",
        name="Required connection Music Assistant",
        icon="mdi:music-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda runtime, entry: _connection(runtime, "music_assistant").get("status") or "unknown",
        attrs_fn=lambda runtime, entry: _connection(runtime, "music_assistant"),
    ),
    HomeiiFlowSensorDescription(
        key="required_connection_queue",
        name="Required connection Queue",
        icon="mdi:playlist-music",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda runtime, entry: _connection(runtime, "queue_provider").get("status") or "unknown",
        attrs_fn=lambda runtime, entry: _connection(runtime, "queue_provider"),
    ),
    HomeiiFlowSensorDescription(
        key="required_connection_library",
        name="Required connection Library",
        icon="mdi:library",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda runtime, entry: _connection(runtime, "library_provider").get("status") or "unknown",
        attrs_fn=lambda runtime, entry: _connection(runtime, "library_provider"),
    ),
    HomeiiFlowSensorDescription(
        key="required_connection_search",
        name="Required connection Search",
        icon="mdi:magnify",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda runtime, entry: _connection(runtime, "search_provider").get("status") or "unknown",
        attrs_fn=lambda runtime, entry: _connection(runtime, "search_provider"),
    ),
    HomeiiFlowSensorDescription(
        key="players_total",
        name="Music Assistant players total",
        icon="mdi:speaker-multiple",
        native_unit_of_measurement="players",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.cached_stats().get("players_total", 0),
        attrs_fn=lambda runtime, entry: {
            "all_media_players_total": runtime.cached_stats().get("all_media_players_total", 0),
        },
    ),
    HomeiiFlowSensorDescription(
        key="players_playing",
        name="Players playing",
        icon="mdi:play-circle",
        native_unit_of_measurement="players",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.cached_stats().get("players_playing", 0),
        attrs_fn=lambda runtime, entry: {
            "entities": runtime.cached_stats().get("playing_entities", []),
        },
    ),
    HomeiiFlowSensorDescription(
        key="active_player",
        name="Active player",
        icon="mdi:speaker-play",
        value_fn=lambda runtime, entry: runtime.cached_stats().get("active_player_entity") or "none",
        attrs_fn=lambda runtime, entry: {
            "active_player": runtime.cached_stats().get("active_player") or {},
            "playing_entities": runtime.cached_stats().get("playing_entities", []),
        },
    ),
    HomeiiFlowSensorDescription(
        key="players_grouped",
        name="Grouped players",
        icon="mdi:speaker-wireless",
        native_unit_of_measurement="players",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.cached_stats().get("players_grouped", 0),
        attrs_fn=lambda runtime, entry: {
            "entities": runtime.cached_stats().get("grouped_entities", []),
            "active_queue_groups": runtime.cached_stats().get("active_queue_groups", {}),
        },
    ),
    HomeiiFlowSensorDescription(
        key="music_assistant_players",
        name="Music Assistant players",
        icon="mdi:music-circle",
        native_unit_of_measurement="players",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.cached_stats().get("music_assistant_players", 0),
        attrs_fn=lambda runtime, entry: {
            "entities": runtime.cached_stats().get("music_assistant_entities", []),
        },
    ),
    HomeiiFlowSensorDescription(
        key="schedules",
        name="Schedules",
        icon="mdi:calendar-clock",
        native_unit_of_measurement="schedules",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.schedule_count(_profile_id(entry)),
        attrs_fn=lambda runtime, entry: {
            "next_schedule": runtime.next_schedule_summary(_profile_id(entry)),
            "schedules": runtime.schedule_summaries(_profile_id(entry)),
            "scheduler_status": runtime.orchestration_status(),
            "last_schedule_check": runtime.orchestration_status().get("last_schedule_check"),
            "last_schedule_action": runtime.orchestration_status().get("last_schedule_action"),
        },
    ),
    HomeiiFlowSensorDescription(
        key="next_schedule",
        name="Next schedule",
        icon="mdi:calendar-start",
        value_fn=lambda runtime, entry: runtime.next_schedule_summary(_profile_id(entry)).get("next_run") or "none",
        attrs_fn=lambda runtime, entry: {
            "next_schedule": runtime.next_schedule_summary(_profile_id(entry)),
            "schedules": runtime.schedule_summaries(_profile_id(entry)),
            "scheduler_status": runtime.orchestration_status(),
            "last_schedule_check": runtime.orchestration_status().get("last_schedule_check"),
            "last_schedule_action": runtime.orchestration_status().get("last_schedule_action"),
        },
    ),
    HomeiiFlowSensorDescription(
        key="timers",
        name="Timers",
        icon="mdi:timer-outline",
        native_unit_of_measurement="timers",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.timer_count(_profile_id(entry)),
        attrs_fn=lambda runtime, entry: {
            "next_timer": runtime.next_timer_summary(_profile_id(entry)),
            "timers": runtime.timer_summaries(_profile_id(entry)),
            "last_timer_action": runtime.orchestration_status().get("last_timer_action"),
        },
    ),
    HomeiiFlowSensorDescription(
        key="next_timer",
        name="Next timer",
        icon="mdi:timer-play-outline",
        value_fn=lambda runtime, entry: runtime.next_timer_summary(_profile_id(entry)).get("ends_at") or "none",
        attrs_fn=lambda runtime, entry: {
            "next_timer": runtime.next_timer_summary(_profile_id(entry)),
            "timers": runtime.timer_summaries(_profile_id(entry)),
            "last_timer_action": runtime.orchestration_status().get("last_timer_action"),
        },
    ),
    HomeiiFlowSensorDescription(
        key="volume_rules",
        name="Volume rules",
        icon="mdi:volume-high",
        native_unit_of_measurement="rules",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.volume_rule_count(_profile_id(entry)),
        attrs_fn=lambda runtime, entry: {
            "volume_rules": runtime.volume_rule_summaries(_profile_id(entry)),
            "active_volume_rules": runtime.active_volume_rule_summaries(_profile_id(entry)),
            "last_volume_action": runtime.orchestration_status().get("last_volume_action"),
        },
    ),
    HomeiiFlowSensorDescription(
        key="announcements",
        name="Announcements",
        icon="mdi:bullhorn-outline",
        native_unit_of_measurement="announcements",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.announcement_count(_profile_id(entry)),
        attrs_fn=lambda runtime, entry: {
            "last_announcement": runtime.orchestration_status().get("last_announcement_action"),
            "recent": runtime.announcements(_profile_id(entry))[:5],
        },
    ),
    HomeiiFlowSensorDescription(
        key="activity",
        name="Activity",
        icon="mdi:history",
        native_unit_of_measurement="events",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.activity_count(_profile_id(entry)),
        attrs_fn=lambda runtime, entry: {
            "last_activity": runtime.last_activity(_profile_id(entry)),
            "recent": runtime.activity(_profile_id(entry))[:10],
        },
    ),
    HomeiiFlowSensorDescription(
        key="last_activity",
        name="Last activity",
        icon="mdi:timeline-clock-outline",
        value_fn=lambda runtime, entry: runtime.last_activity(_profile_id(entry)).get("message") or "none",
        attrs_fn=lambda runtime, entry: {
            "last_activity": runtime.last_activity(_profile_id(entry)),
        },
    ),
    HomeiiFlowSensorDescription(
        key="playback_today",
        name="Playback today",
        icon="mdi:chart-timeline-variant",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.playback_statistics().get("today_minutes", 0),
        attrs_fn=lambda runtime, entry: runtime.playback_statistics(),
        force_update=True,
    ),
    HomeiiFlowSensorDescription(
        key="playback_sessions_today",
        name="Playback sessions today",
        icon="mdi:counter",
        native_unit_of_measurement="sessions",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda runtime, entry: runtime.playback_statistics().get("today_sessions", 0),
        attrs_fn=lambda runtime, entry: {
            "players_today": runtime.playback_statistics().get("players_today", []),
            "top_player_today": runtime.playback_statistics().get("top_player_today", {}),
        },
        force_update=True,
    ),
    HomeiiFlowSensorDescription(
        key="top_player_today",
        name="Top player today",
        icon="mdi:trophy-outline",
        value_fn=lambda runtime, entry: runtime.playback_statistics().get("top_player_today", {}).get("friendly_name") or "none",
        attrs_fn=lambda runtime, entry: runtime.playback_statistics().get("top_player_today", {}),
        force_update=True,
    ),
    HomeiiFlowSensorDescription(
        key="screensaver_recommendation",
        name="Screensaver recommendation",
        icon="mdi:monitor-screenshot",
        value_fn=lambda runtime, entry: runtime.screensaver_recommendation().get("mode") or "clock",
        attrs_fn=lambda runtime, entry: runtime.screensaver_recommendation(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HOMEii Flow Engine sensors."""
    runtime = async_get_runtime(hass)
    async_add_entities(HomeiiFlowSensor(runtime, entry, description) for description in SENSORS)


class HomeiiFlowSensor(SensorEntity):
    """HOMEii Flow Engine sensor."""

    entity_description: HomeiiFlowSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: HomeiiFlowRuntime,
        entry: ConfigEntry,
        description: HomeiiFlowSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        self._runtime = runtime
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_name = description.name
        self._attr_force_update = description.force_update

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime storage and orchestration updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self.async_write_ha_state)
        )

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
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self._runtime, self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return sensor attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._runtime, self._entry)
