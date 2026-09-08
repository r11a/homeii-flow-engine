"""Validated MA queue switches shared by HA service commands."""
from typing import Any
import math


def build_playback_speed(queue_id: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Set speed on the owning MA queue; reject values MA cannot apply."""
    value = payload.get("speed")
    if not queue_id or isinstance(value, bool):
        raise ValueError("An active queue and numeric playback speed are required")
    try:
        speed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Playback speed must be a number between 0.5 and 3.0") from error
    if not math.isfinite(speed) or not 0.5 <= speed <= 3.0:
        raise ValueError("Playback speed must be between 0.5 and 3.0")
    return "player_queues/set_playback_speed", {"queue_id": queue_id, "speed": speed}

_QUEUE_SWITCHES = {
    "autoplay": ("player_queues/autoplay", "autoplay_enabled"),
    "autoplay_set": ("player_queues/autoplay", "autoplay_enabled"),
    "dont_stop_the_music": ("player_queues/autoplay", "autoplay_enabled"),
    "crossfade": ("player_queues/crossfade", "crossfade_enabled"),
    "crossfade_set": ("player_queues/crossfade", "crossfade_enabled"),
}

def build_queue_switch(command: str, queue_id: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build one explicit queue mutation without interpreting strings as booleans."""
    if command not in _QUEUE_SWITCHES:
        raise ValueError(f"Unsupported queue switch: {command}")
    if not queue_id:
        raise ValueError("An active queue is required")
    api_command, field = _QUEUE_SWITCHES[command]
    value = payload.get(field, payload.get("enabled", True))
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return api_command, {"queue_id": queue_id, field: value}
