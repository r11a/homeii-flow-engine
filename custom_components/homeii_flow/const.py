"""Constants for HOMEii Flow Engine."""

from __future__ import annotations

DOMAIN = "homeii_flow"
NAME = "HOMEii Flow Engine"
VERSION = "1.0.0-beta.1"

MUSIC_ASSISTANT_SCHEMA_MIN = 63
MUSIC_ASSISTANT_SCHEMA_VALIDATED = 63

CONF_INSTANCE_ID = "instance_id"
CONF_PROFILE_ID = "profile_id"
CONF_ENABLE_EXPERIMENTAL = "enable_experimental"
CONF_MUSIC_ASSISTANT_URL = "music_assistant_url"
CONF_MUSIC_ASSISTANT_EXTERNAL_URL = "music_assistant_external_url"
CONF_MUSIC_ASSISTANT_TOKEN = "music_assistant_token"

DEFAULT_INSTANCE_ID = "default"
DEFAULT_PROFILE_ID = "default"
DEFAULT_NAME = "HOMEii Flow Engine"
PLATFORMS = ["binary_sensor", "button", "calendar", "number", "sensor", "switch"]

STORAGE_KEY = f"{DOMAIN}.storage"
STORAGE_VERSION = 1
MEDIA_CACHE_STORAGE_KEY = f"{DOMAIN}.media_cache"
MEDIA_CACHE_STORAGE_VERSION = 2

EVENT_ENGINE_ANNOUNCEMENT = f"{DOMAIN}_announcement"
EVENT_ENGINE_GROUP_APPLY = f"{DOMAIN}_group_apply"
EVENT_MUSIC_ASSISTANT = f"{DOMAIN}_music_assistant_event"
SIGNAL_ENGINE_UPDATED = f"{DOMAIN}_updated"

CAPABILITIES = {
    "context": True,
    "bootstrap_snapshot": True,
    "persistent_media_cache": True,
    "persistent_media_detail_cache": True,
    "stale_while_revalidate": True,
    "request_coalescing": True,
    "queue_request_coalescing": True,
    "short_queue_cache": True,
    "compatible_library_shelves": True,
    "compact_library_responses": True,
    "diagnostics": True,
    "required_connections": True,
    "music_assistant_health": True,
    "stats": True,
    "playback_statistics": True,
    "orchestration": True,
    "players": True,
    "playback_proxy": True,
    "player_commands": True,
    "queue_proxy": True,
    "queue_source_of_truth": True,
    "revisioned_snapshots": True,
    "snapshot_epoch": True,
    "active_source_contract": True,
    "atomic_queue_state": True,
    "queue_action": True,
    "queue_artwork_proxy": True,
    "queue_transfer": True,
    "queue_autoplay": True,
    "queue_crossfade": True,
    "queue_settings": True,
    "queue_playback_speed": True,
    "ai_radio_dj": True,
    "playlist_editing": True,
    "library_proxy": True,
    "library_pagination": True,
    "favorites_aggregate": True,
    "favorite_mutation": True,
    "favorite_revision_invalidation": True,
    "search_proxy": True,
    "search_source_of_truth": True,
    "music_assistant_command_bridge": True,
    "music_assistant_direct_api": True,
    "music_assistant_authenticated_api": True,
    "music_assistant_websocket_commands": True,
    "music_assistant_realtime_events": True,
    "music_assistant_server_info": True,
    "music_assistant_schema_63": True,
    "music_assistant_queue_resolution": True,
    "music_assistant_extended_library": True,
    "music_assistant_2_10": True,
    "typed_music_assistant_contract": True,
    "direct_player_catalog": True,
    "direct_library_catalog": True,
    "direct_provider_search": True,
    "full_queue_snapshots": True,
    "group_apply": True,
    "schedules": True,
    "schedule_management": True,
    "schedule_execution": True,
    "schedule_calendar": True,
    "system_screensaver": True,
    "system_screensaver_show": True,
    "system_screensaver_artwork_proxy": True,
    "item_artwork_proxy": True,
    "stable_artwork_urls": True,
    "artwork_etag": True,
    "frontend_static": True,
    "timers": True,
    "timer_management": True,
    "timer_execution": True,
    "volume_rules": True,
    "volume_rule_management": True,
    "volume_rule_enforcement": True,
    "announcements": True,
    "announcement_dispatch": True,
    "activity_log": True,
    "sendspin_status": True,
    "sendspin_bridge": True,
}
