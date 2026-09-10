"""Normalize paired playback clocks from MA player and current-media snapshots."""
from datetime import UTC, datetime
import math


def playback_position_pair(raw):
    candidates = []
    for source in (raw, raw.get("current_media") or raw.get("media") or {}):
        position = source.get("elapsed_time", source.get("media_position"))
        try:
            position = float(position)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(position):
            continue
        stamp = source.get("elapsed_time_last_updated", source.get("media_position_updated_at"))
        updated = 0.0
        try:
            updated = float(stamp)
            if updated > 1e12:
                updated /= 1000
            if not math.isfinite(updated) or updated < 0:
                updated = 0.0
        except (ValueError, TypeError):
            try:
                parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                updated = parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
            except (ValueError, TypeError):
                pass
        candidates.append((max(0.0, position), updated))
    position, updated = max(candidates, key=lambda pair: pair[1], default=(0.0, 0.0))
    return position, datetime.fromtimestamp(updated, tz=UTC).isoformat() if updated else None
