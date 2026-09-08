"""Diagnostics support for HOMEii Flow Engine."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import async_get_runtime
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = async_get_runtime(hass)
    profile_id = entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID)
    return {
        "version": VERSION,
        "entry": {
            "title": entry.title,
            "instance_id": entry.data.get(CONF_INSTANCE_ID),
            "profile_id": profile_id,
            "state": str(entry.state),
        },
        "context": runtime.context(
            instance_id=entry.data.get(CONF_INSTANCE_ID),
            profile_id=profile_id,
        ),
        "stats": runtime.stats(),
        "playback_statistics": runtime.playback_statistics(),
        "media_cache": runtime.library_cache_status(),
        "screensaver": runtime.screensaver_state(profile_id),
        "orchestration": runtime.orchestration_status(),
        "services": runtime.services_snapshot(),
        "required_connections": runtime.required_connections_snapshot(),
        "schedules": runtime.schedule_summaries(profile_id),
        "next_schedule": runtime.next_schedule_summary(profile_id),
        "timers": runtime.timer_summaries(profile_id),
        "next_timer": runtime.next_timer_summary(profile_id),
        "volume_rules": runtime.volume_rule_summaries(profile_id),
        "active_volume_rules": runtime.active_volume_rule_summaries(profile_id),
        "activity": runtime.activity(profile_id)[:10],
        "last_activity": runtime.last_activity(profile_id),
        "platforms": ["binary_sensor", "button", "calendar", "number", "sensor", "switch"],
        "sensor_keys": [
            "status",
            "required_connections",
            "required_connection_music_assistant",
            "required_connection_queue",
            "required_connection_library",
            "required_connection_search",
            "players_total",
            "players_playing",
            "active_player",
            "players_grouped",
            "music_assistant_players",
            "schedules",
            "next_schedule",
            "timers",
            "next_timer",
            "volume_rules",
            "announcements",
            "activity",
            "last_activity",
            "playback_today",
            "playback_sessions_today",
            "top_player_today",
            "screensaver_recommendation",
        ],
        "binary_sensor_keys": [
            "required_connections_ok",
            "music_assistant_connected",
            "queue_provider_connected",
            "library_provider_connected",
            "search_provider_connected",
            "any_player_playing",
            "group_active",
            "volume_policy_active",
        ],
        "button_keys": [
            "refresh_state",
            "run_orchestration",
            "apply_volume_rules",
            "run_next_schedule",
            "run_schedule",
        ],
        "calendar_keys": [
            "schedules_calendar",
        ],
        "number_keys": [
            "volume_rule_max",
        ],
        "switch_keys": [
            "system_screensaver",
            "schedule",
            "timer",
            "volume_rule",
        ],
        "schedules_count": len(runtime.schedules(profile_id)),
        "timers_count": len(runtime.timers(profile_id)),
        "volume_rules_count": len(runtime.volume_rules(profile_id)),
        "announcements_count": len(runtime.announcements(profile_id)),
        "activity_count": runtime.activity_count(profile_id),
    }
