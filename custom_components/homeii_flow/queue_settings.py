"""Narrow, server-validated access to Music Assistant queue preferences."""
from __future__ import annotations

FIELD_TYPES = {
    "autoplay_enabled": bool, "autoplay_mode": str, "autoplay_playlist": str,
    "smart_shuffle_enabled": str, "smart_shuffle_optimize_smart_fades": str,
    "crossfade_enabled": bool, "crossfade_mode": str, "crossfade_duration": int,
}
FIELDS = frozenset(FIELD_TYPES)


def supported_entries(config):
    """Return only presentation-safe fields supported by the connected MA version."""
    raw = config.get("values", {}) if isinstance(config, dict) else {}
    return {key: {name: entry.get(name) for name in (
        "key", "type", "value", "default_value", "options", "range", "read_only",
        "label", "description", "depends_on", "depends_on_value",
    )} for key, entry in raw.items()
        if key in FIELDS and isinstance(entry, dict) and not entry.get("hidden")}


def validate_changes(values, entries):
    """Validate a partial patch against the live server contract."""
    if not isinstance(values, dict) or not values:
        raise ValueError("At least one queue preference is required")
    if set(values) - FIELDS:
        raise ValueError("Unsupported queue preference")
    clean = {}
    for key, value in values.items():
        entry = entries.get(key)
        if not entry or entry.get("read_only"):
            raise ValueError(f"Queue preference is unavailable: {key}")
        kind = entry.get("type")
        if kind == "boolean" and type(value) is not bool:
            raise ValueError(f"{key} must be boolean")
        if kind == "integer" and type(value) is not int:
            raise ValueError(f"{key} must be integer")
        if kind == "string" and not isinstance(value, str):
            raise ValueError(f"{key} must be string")
        options = entry.get("options") or []
        if options and value not in [item.get("value") for item in options if not item.get("disabled")]:
            raise ValueError(f"Invalid option for {key}")
        bounds = entry.get("range")
        if bounds and kind == "integer" and not bounds[0] <= value <= bounds[1]:
            raise ValueError(f"{key} is outside the supported range")
        if key == "autoplay_playlist" and (len(value) > 2048 or "://" not in value):
            raise ValueError("Choose a playlist with a valid Music Assistant URI")
        clean[key] = value
    mode = clean.get("autoplay_mode", entries.get("autoplay_mode", {}).get("value"))
    playlist = clean.get("autoplay_playlist", entries.get("autoplay_playlist", {}).get("value"))
    if mode == "playlist" and not playlist:
        raise ValueError("Choose an Autoplay playlist before saving")
    return clean


async def async_queue_settings(client, values=None):
    """Read or save only queue preferences, with an authoritative confirmation."""
    config = await client.async_command("config/core/get", {"domain": "player_queues"}, timeout=20)
    entries = supported_entries(config)
    if values is not None:
        clean = validate_changes(values, entries)
        # Never retry writes: a timeout can occur after MA has already saved.
        await client.async_command("config/core/save", {"domain": "player_queues", "values": clean}, timeout=20)
        config = await client.async_command("config/core/get", {"domain": "player_queues"}, timeout=20)
        entries = supported_entries(config)
        if any(entries.get(key, {}).get("value") != value for key, value in clean.items()):
            raise ValueError("Music Assistant did not confirm the saved queue preferences")
    return {"scope": "music_assistant_global", "entries": entries, "saved": values is not None}
