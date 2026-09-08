"""Runtime state and backend helpers for HOMEii Flow Engine."""

from __future__ import annotations

from .queue_settings import async_queue_settings
from .queue_controls import build_queue_switch, build_playback_speed

import asyncio
import copy
import hashlib
import logging
import time

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aiohttp import ClientError, ClientTimeout

from homeassistant.components import tts
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CAPABILITIES,
    DEFAULT_INSTANCE_ID,
    DEFAULT_PROFILE_ID,
    DOMAIN,
    EVENT_ENGINE_ANNOUNCEMENT,
    EVENT_ENGINE_GROUP_APPLY,
    EVENT_MUSIC_ASSISTANT,
    MEDIA_CACHE_STORAGE_KEY,
    MEDIA_CACHE_STORAGE_VERSION,
    MUSIC_ASSISTANT_SCHEMA_MIN,
    SIGNAL_ENGINE_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
    VERSION,
)
from .exceptions import HomeiiFlowServiceUnavailable
from .ma_client import MusicAssistantEventClient

_LOGGER = logging.getLogger(__name__)


def _utc_iso() -> str:
    """Return a UTC timestamp."""
    return datetime.now(UTC).isoformat()


def _local_datetime(value: datetime | None = None) -> datetime:
    """Return a Home Assistant local datetime."""
    if value is None:
        return dt_util.now()
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return dt_util.as_local(current)


def _parse_utc_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp into UTC."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_id_part(value: Any, fallback: str = "item") -> str:
    """Return a storage-safe id fragment."""
    clean = "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip().lower())
    clean = "_".join(part for part in clean.split("_") if part)
    return clean or fallback


def _safe_list(value: Any) -> list[Any]:
    """Normalize a value into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _first_non_empty(*values: Any) -> Any:
    """Return the first value that is not empty."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _clean_string(value: Any) -> str:
    """Return a stripped string value."""
    return str(value or "").strip()


def _maybe_number(value: Any) -> float | None:
    """Return a finite number when possible."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _append_unique(items: list[Any], value: Any) -> None:
    """Append a non-empty value once while preserving order."""
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    if value not in items:
        items.append(value)


def _dict_first(source: dict[str, Any], *keys: str) -> Any:
    """Return the first non-empty value from a dictionary."""
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return an integer constrained to a safe range."""
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return max(minimum, min(maximum, numeric))


def _normalized_http_url(value: Any) -> str:
    """Return a normalized HTTP(S) URL without a trailing slash."""
    clean = str(value or "").strip()
    if not clean.startswith(("http://", "https://")):
        return ""
    return clean.rstrip("/")


MEDIA_GROUP_ALIASES = {
    "album": "album",
    "albums": "album",
    "artist": "artist",
    "artists": "artist",
    "genre": "genre",
    "genres": "genre",
    "item": "track",
    "items": "",
    "library_items": "",
    "media": "",
    "media_items": "",
    "playlist": "playlist",
    "playlists": "playlist",
    "podcast": "podcast",
    "podcasts": "podcast",
    "radio": "radio",
    "radios": "radio",
    "result": "",
    "results": "",
    "response": "",
    "track": "track",
    "tracks": "track",
}


MEDIA_ITEM_MARKER_KEYS = {
    "album",
    "album_name",
    "artist",
    "artists",
    "duration",
    "favorite",
    "image",
    "image_url",
    "item_id",
    "label",
    "media_album_name",
    "media_artist",
    "media_content_id",
    "media_content_type",
    "media_duration",
    "media_item",
    "media_title",
    "media_type",
    "metadata",
    "name",
    "provider",
    "provider_domain",
    "provider_instance",
    "provider_label",
    "provider_name",
    "thumbnail",
    "title",
    "type",
    "uri",
}


WRAPPER_KEYS = {
    "data",
    "item",
    "items",
    "library_items",
    "media",
    "media_items",
    "result",
    "results",
    "response",
}


def _looks_like_media_item(value: Any) -> bool:
    """Return true when a dictionary looks like a single media item."""
    if not isinstance(value, dict):
        return False
    keys = set(value)
    if keys & {"uri", "media_id", "media_content_id", "item_id"}:
        return True
    if keys & {"name", "title", "media_title", "label"} and keys & MEDIA_ITEM_MARKER_KEYS:
        return True
    if isinstance(value.get("media_item"), dict):
        return True
    return False


def _extract_media_items(value: Any) -> list[dict[str, Any]]:
    """Extract media item dictionaries from common HA and MA response shapes."""
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict) and _looks_like_media_item(item):
                items.append(item)
            else:
                items.extend(_extract_media_items(item))
        return items
    if not isinstance(value, dict):
        return []

    if _looks_like_media_item(value):
        return [value]

    best: list[dict[str, Any]] = []
    for key in ("items", "media", "media_items", "library_items", "data", "result", "results", "response"):
        nested = value.get(key)
        items = _extract_media_items(nested)
        if len(items) > len(best):
            best = items

    grouped: list[dict[str, Any]] = []
    for key, nested in value.items():
        alias = MEDIA_GROUP_ALIASES.get(str(key).lower())
        if alias is None and str(key).lower() not in WRAPPER_KEYS:
            continue
        nested_items = _extract_media_items(nested)
        if nested_items:
            grouped.extend(nested_items)

    if grouped and len(grouped) >= len(best):
        return grouped
    if best:
        return best
    deep_best: list[dict[str, Any]] = []
    for key, nested in value.items():
        key_lower = str(key).lower()
        if key_lower in {"album", "artist", "artists", "metadata", "provider_mappings"}:
            continue
        nested_items = _extract_media_items(nested)
        if len(nested_items) > len(deep_best):
            deep_best = nested_items
    if deep_best:
        return deep_best
    return [value] if _looks_like_media_item(value) else []


def _media_item_id(item: dict[str, Any]) -> str:
    """Return the best playable id from a Music Assistant library item."""
    return str(
        item.get("uri")
        or item.get("media_id")
        or item.get("media_content_id")
        or item.get("item_id")
        or item.get("id")
        or ""
    ).strip()


def _media_item_name(item: dict[str, Any]) -> str:
    """Return a human-friendly name for a media item."""
    return str(
        item.get("name")
        or item.get("title")
        or item.get("media_title")
        or item.get("label")
        or _media_item_id(item)
        or ""
    ).strip()


def _media_item_type(item: dict[str, Any], fallback: str = "playlist") -> str:
    """Return the best media type from a media item."""
    return str(
        item.get("media_type")
        or item.get("media_content_type")
        or item.get("type")
        or fallback
        or "playlist"
    ).strip() or "playlist"


def _schedule_media_mode(value: Any, media_id: str = "") -> str:
    """Normalize how a stored schedule should resolve its media."""
    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in {"random", "random_playlist", "gentle_morning", "morning_mix", "random_gentle_morning_mix"}:
        return "random_playlist"
    if mode in {"selected", "fixed", "explicit", "playlist"}:
        return "selected"
    return "selected" if media_id else "random_playlist"


def _schedule_morning_score(item: dict[str, Any]) -> int:
    """Return a light preference score for wake-friendly playlists."""
    haystack = " ".join(
        str(value or "")
        for value in (
            item.get("name"),
            item.get("title"),
            item.get("description"),
            (item.get("metadata") or {}).get("description") if isinstance(item.get("metadata"), dict) else "",
            item.get("provider_label"),
        )
    ).lower()
    keywords = (
        "morning",
        "sunrise",
        "coffee",
        "breakfast",
        "wake",
        "wakeup",
        "wake up",
        "calm",
        "soft",
        "easy",
        "acoustic",
        "chill",
        "lofi",
        "lo-fi",
        "pleasant",
    )
    return sum(1 for keyword in keywords if keyword in haystack)


def _player_playback_snapshot(state: Any | None) -> dict[str, Any]:
    """Return a compact playback snapshot for diagnostics."""
    if state is None:
        return {"exists": False}
    attrs = state.attributes or {}
    return {
        "exists": True,
        "state": state.state,
        "media_title": attrs.get("media_title"),
        "media_content_id": attrs.get("media_content_id"),
        "media_content_type": attrs.get("media_content_type"),
        "active_queue": attrs.get("active_queue") or attrs.get("queue_id"),
        "app_id": attrs.get("app_id"),
        "source": attrs.get("source"),
    }


def _playback_snapshot_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Return whether a player snapshot suggests playback changed."""
    if not after.get("exists"):
        return False
    after_state = str(after.get("state") or "").lower()
    if after_state != "playing":
        return False
    return before.get("state") != "playing" or any(
        after.get(key) and after.get(key) != before.get(key)
        for key in ("media_title", "media_content_id", "active_queue")
    )


def _parse_hhmm(value: Any) -> tuple[int, int] | None:
    """Parse a HH:MM value."""
    if not isinstance(value, str) or ":" not in value:
        return None
    hour_raw, minute_raw = value.split(":", 1)
    try:
        hour = int(hour_raw)
        minute = int(minute_raw)
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def _minutes(hour: int, minute: int) -> int:
    """Return minutes since midnight."""
    return hour * 60 + minute


def _homeii_weekday(now: datetime) -> int:
    """Return weekday where Sunday is 0."""
    return (now.weekday() + 1) % 7


def _schedule_days(schedule: dict[str, Any]) -> list[int]:
    """Return normalized HOMEii weekday indexes for a schedule."""
    days: list[int] = []
    for raw_day in _safe_list(schedule.get("days")):
        if not str(raw_day).strip():
            continue
        try:
            day = int(raw_day)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return days


def _due_schedule_datetime(schedule: dict[str, Any], now: datetime) -> datetime | None:
    """Return the local schedule datetime that is due now, if any."""
    if not bool(schedule.get("enabled", True)):
        return None
    schedule_time = _parse_hhmm(schedule.get("time"))
    if schedule_time is None:
        return None
    days = _schedule_days(schedule)
    for day_offset in (0, -1):
        candidate = (now + timedelta(days=day_offset)).replace(
            hour=schedule_time[0],
            minute=schedule_time[1],
            second=0,
            microsecond=0,
        )
        if days and _homeii_weekday(candidate) not in days:
            continue
        delta_seconds = (now - candidate).total_seconds()
        if 0 <= delta_seconds < 120:
            return candidate
    return None


def _next_schedule_datetime(
    schedule: dict[str, Any],
    now: datetime,
    *,
    include_due: bool = True,
) -> datetime | None:
    """Return the next local datetime when a schedule should run."""
    if include_due:
        due_at = _due_schedule_datetime(schedule, now)
        if due_at is not None:
            return due_at
    if not bool(schedule.get("enabled", True)):
        return None
    schedule_time = _parse_hhmm(schedule.get("time"))
    if schedule_time is None:
        return None
    days = _schedule_days(schedule)
    for day_offset in range(8):
        candidate = (now + timedelta(days=day_offset)).replace(
            hour=schedule_time[0],
            minute=schedule_time[1],
            second=0,
            microsecond=0,
        )
        if days and _homeii_weekday(candidate) not in days:
            continue
        if candidate >= now:
            return candidate
    return None


def _time_window_active(now: datetime, start_time: str, end_time: str) -> bool:
    """Return whether the current local time is inside an optional time window."""
    start = _parse_hhmm(start_time)
    end = _parse_hhmm(end_time)
    if start is None and end is None:
        return True
    current = _minutes(now.hour, now.minute)
    if start is not None and end is None:
        return current >= _minutes(*start)
    if start is None and end is not None:
        return current <= _minutes(*end)
    start_minutes = _minutes(*start)
    end_minutes = _minutes(*end)
    if start_minutes <= end_minutes:
        return start_minutes <= current <= end_minutes
    return current >= start_minutes or current <= end_minutes


@dataclass(slots=True)
class EngineEntry:
    """Loaded HOMEii Flow Engine config entry."""

    entry_id: str
    instance_id: str = DEFAULT_INSTANCE_ID
    profile_id: str = DEFAULT_PROFILE_ID
    title: str = "HOMEii Flow Engine"
    enable_experimental: bool = False
    music_assistant_url: str = ""
    music_assistant_external_url: str = ""
    music_assistant_token: str = field(default="", repr=False)
    loaded_at: str = field(default_factory=_utc_iso)

    def as_dict(self) -> dict[str, Any]:
        """Return serializable entry info."""
        return {
            "entry_id": self.entry_id,
            "instance_id": self.instance_id,
            "profile_id": self.profile_id,
            "title": self.title,
            "enable_experimental": self.enable_experimental,
            "music_assistant_url_configured": bool(self.music_assistant_url),
            "music_assistant_external_url_configured": bool(self.music_assistant_external_url),
            "music_assistant_token_configured": bool(self.music_assistant_token),
            "loaded_at": self.loaded_at,
        }


class HomeiiScheduleActionQueue:
    """Execute one scheduled playback action with availability handling."""

    def __init__(self, runtime: "HomeiiFlowRuntime", schedule_id: str) -> None:
        """Initialize an action queue for one schedule."""
        self.runtime = runtime
        self.schedule_id = schedule_id
        self.queue_busy = False

    async def async_run(
        self,
        schedule: dict[str, Any],
        *,
        due_at: datetime,
        trigger: str,
    ) -> dict[str, Any]:
        """Run the schedule when its target player and services are ready."""
        if self.queue_busy:
            return {
                "ok": False,
                "schedule_id": self.schedule_id,
                "phase": "queue",
                "trigger": trigger,
                "error": "schedule action queue is already running",
                "executed_at": _utc_iso(),
            }

        self.queue_busy = True
        player = str(schedule.get("player") or "").strip()
        max_attempts = _bounded_int(schedule.get("runner_retry_attempts"), 6, 1, 24)
        retry_delay = _bounded_int(schedule.get("runner_retry_delay"), 10, 1, 60)
        availability_attempts: list[dict[str, Any]] = []
        try:
            for attempt_index in range(max_attempts):
                readiness = self.runtime._player_readiness(player)
                attempt = {
                    "attempt": attempt_index + 1,
                    "at": _utc_iso(),
                    "readiness": readiness,
                }
                availability_attempts.append(attempt)
                if readiness.get("ready"):
                    result = await self.runtime.async_execute_schedule(schedule)
                    result["schedule_id"] = schedule.get("id") or self.schedule_id
                    result["trigger"] = trigger
                    result["due_at"] = due_at.isoformat()
                    result["availability_attempts"] = availability_attempts
                    result["runner"] = "homeii_schedule_action_queue"
                    return result
                if attempt_index < max_attempts - 1:
                    await self._wait_for_player_or_delay(player, retry_delay)
            return {
                "ok": False,
                "schedule_id": schedule.get("id") or self.schedule_id,
                "phase": "availability",
                "trigger": trigger,
                "player": player,
                "due_at": due_at.isoformat(),
                "availability_attempts": availability_attempts,
                "error": str(availability_attempts[-1].get("readiness", {}).get("reason") or "player is not ready"),
                "executed_at": _utc_iso(),
            }
        finally:
            self.queue_busy = False

    async def _wait_for_player_or_delay(self, player: str, seconds: int) -> None:
        """Wait for a player state change or fall back to a simple delay."""
        if not player:
            await asyncio.sleep(seconds)
            return
        ready = asyncio.Event()

        @callback
        def state_changed(_event: Any) -> None:
            if self.runtime._player_readiness(player).get("ready"):
                ready.set()

        unsubscribe = async_track_state_change_event(self.runtime.hass, [player], state_changed)
        try:
            await asyncio.wait_for(ready.wait(), timeout=seconds)
        except TimeoutError:
            return
        finally:
            unsubscribe()


class HomeiiScheduleRunner:
    """Own the timer and action queue for one HOMEii schedule."""

    def __init__(self, manager: "HomeiiScheduleManager", schedule: dict[str, Any]) -> None:
        """Initialize a runner for one schedule."""
        self.manager = manager
        self.runtime = manager.runtime
        self.schedule: dict[str, Any] = dict(schedule)
        self.key = self.runtime._schedule_storage_key(schedule)
        self.schedule_id = str(schedule.get("id") or schedule.get("schedule_id") or "").strip()
        self.profile_id = str(schedule.get("profile_id") or DEFAULT_PROFILE_ID)
        self._timer_unsub: Callable[[], None] | None = None
        self.action_queue = HomeiiScheduleActionQueue(self.runtime, self.schedule_id)
        self.next_run: datetime | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_triggered_at = ""

    def update(self, schedule: dict[str, Any]) -> None:
        """Update schedule data held by this runner."""
        self.schedule = dict(schedule)
        self.key = self.runtime._schedule_storage_key(schedule)
        self.schedule_id = str(schedule.get("id") or schedule.get("schedule_id") or "").strip()
        self.profile_id = str(schedule.get("profile_id") or DEFAULT_PROFILE_ID)

    def cancel(self) -> None:
        """Cancel the point-in-time timer."""
        if self._timer_unsub is not None:
            self._timer_unsub()
            self._timer_unsub = None
        self.next_run = None
        self.runtime._schedule_unsubs.pop(self.key, None)

    def reschedule(self, now: datetime | None = None) -> None:
        """Schedule the next exact run for this schedule."""
        self.cancel()
        local_now = _local_datetime(now)
        if not self.schedule_id or not bool(self.schedule.get("enabled", True)):
            return

        lookup_now = local_now
        due_at = _due_schedule_datetime(self.schedule, local_now)
        if due_at is not None:
            run_key = self.runtime._schedule_run_key(self.schedule, due_at)
            if self.runtime._last_schedule_runs.get(self.key) != run_key:
                self.runtime.hass.async_create_task(self.async_fire(due_at, trigger="catchup"))
                return
            lookup_now = local_now + timedelta(seconds=121)

        next_run = _next_schedule_datetime(self.schedule, lookup_now, include_due=False)
        if next_run is None:
            return

        run_at = next_run if next_run.tzinfo is not None else next_run.replace(tzinfo=UTC)
        run_at_utc = run_at.astimezone(UTC)
        self.next_run = next_run

        @callback
        def timer_finished(now_value: datetime) -> None:
            self.runtime.hass.async_create_task(self.async_fire(_local_datetime(now_value), trigger="timer"))

        self._timer_unsub = async_track_point_in_time(self.runtime.hass, timer_finished, run_at_utc)
        self.runtime._schedule_unsubs[self.key] = self._timer_unsub

    async def async_fire(self, due_at: datetime, *, trigger: str) -> dict[str, Any]:
        """Execute the schedule and immediately schedule its next run."""
        schedule = dict(self.schedule)
        run_at = _local_datetime(due_at)
        run_key = self.runtime._schedule_run_key(schedule, run_at)
        if self.runtime._last_schedule_runs.get(self.key) == run_key:
            result = {
                "ok": True,
                "skipped": True,
                "schedule_id": self.schedule_id,
                "profile_id": self.profile_id,
                "phase": "dedupe",
                "trigger": trigger,
                "due_at": run_at.isoformat(),
                "message": "schedule already ran for this due time",
                "executed_at": _utc_iso(),
            }
        else:
            self.last_triggered_at = _utc_iso()
            # Reserve before yielding so switch/interval callbacks cannot replay this run.
            self.runtime._last_schedule_runs[self.key] = run_key
            result = await self.action_queue.async_run(schedule, due_at=run_at, trigger=trigger)
            if result.get("ok") and str(schedule.get("after_run") or "") == "disable":
                schedule["enabled"] = False
                self.runtime._storage["schedules"] = [
                    schedule
                    if existing.get("profile_id") == schedule.get("profile_id") and existing.get("id") == schedule.get("id")
                    else existing
                    for existing in self.runtime.schedules()
                ]
                await self.runtime.async_save()

        self.last_result = result
        self.runtime._last_schedule_action = result
        self.runtime._last_schedule_check = {
            "checked_at": _utc_iso(),
            "local_time": _local_datetime().isoformat(),
            "local_weekday": _homeii_weekday(_local_datetime()),
            "trigger": trigger,
            "schedule_count": len(self.runtime.schedules()),
            "due_schedule_ids": [self.schedule_id] if self.schedule_id else [],
            "attempted_count": 0 if result.get("skipped") else 1,
            "executed_count": 1 if result.get("ok") and not result.get("skipped") else 0,
            "failed_count": 1 if not result.get("ok") else 0,
            "scheduled_jobs": len(self.runtime._schedule_unsubs),
            "due_at": run_at.isoformat(),
        }
        self.manager.reschedule_all()
        self.manager.write_status()
        async_dispatcher_send(self.runtime.hass, SIGNAL_ENGINE_UPDATED)
        return result

    def details(self) -> dict[str, Any]:
        """Return serializable runner state."""
        return {
            "profile_id": self.profile_id,
            "schedule_id": self.schedule_id,
            "name": self.schedule.get("name"),
            "player": self.schedule.get("player"),
            "media_id": self.schedule.get("media_id") or self.schedule.get("playlist") or "",
            "media_name": self.schedule.get("media_name") or self.schedule.get("playlist_name") or "",
            "next_run": self.next_run.isoformat() if self.next_run else "",
            "next_run_utc": self.next_run.astimezone(UTC).isoformat() if self.next_run and self.next_run.tzinfo else "",
            "last_triggered_at": self.last_triggered_at,
            "queue_busy": self.action_queue.queue_busy,
            "last_result": self.last_result,
        }


class HomeiiScheduleManager:
    """Manage schedule runners using a scheduler-component-style lifecycle."""

    def __init__(self, runtime: "HomeiiFlowRuntime") -> None:
        """Initialize the schedule manager."""
        self.runtime = runtime
        self.state = "stopped"
        self.started_at = ""
        self.ready_at = ""
        self._ready_unsub: Callable[[], None] | None = None
        self._runners: dict[str, HomeiiScheduleRunner] = {}

    def start(self) -> None:
        """Start the manager and delay ready state until HA entities settle."""
        if self.state in {"init", "ready"}:
            self.reschedule_all()
            return
        self.state = "init"
        self.started_at = _local_datetime().isoformat()
        self.ready_at = ""

        @callback
        def mark_ready(_now: datetime) -> None:
            self.state = "ready"
            self.ready_at = _local_datetime().isoformat()
            self.reschedule_all()
            self.runtime.hass.async_create_task(self.runtime.async_tick_orchestration(trigger="scheduler_ready"))
            async_dispatcher_send(self.runtime.hass, SIGNAL_ENGINE_UPDATED)

        self._ready_unsub = async_call_later(self.runtime.hass, 10, mark_ready)

    def stop(self) -> None:
        """Stop all schedule runners."""
        if self._ready_unsub is not None:
            self._ready_unsub()
            self._ready_unsub = None
        for runner in list(self._runners.values()):
            runner.cancel()
        self._runners = {}
        self.state = "stopped"
        self.write_status()

    def reschedule_all(self) -> None:
        """Create, update, and remove runners for current schedule storage."""
        if self.state == "stopped":
            return
        local_now = _local_datetime()
        self.runtime._last_reschedule_at = local_now.isoformat()
        current: dict[str, dict[str, Any]] = {}
        for schedule in self.runtime.schedules():
            key = self.runtime._schedule_storage_key(schedule)
            current[key] = schedule
            runner = self._runners.get(key)
            if runner is None:
                runner = HomeiiScheduleRunner(self, schedule)
                self._runners[key] = runner
            else:
                runner.update(schedule)
            if self.state == "ready":
                runner.reschedule(local_now)
        for stale_key in [key for key in self._runners if key not in current]:
            self._runners[stale_key].cancel()
            self._runners.pop(stale_key, None)
        self.write_status()

    async def async_run_due(self, now: datetime | None = None, *, trigger: str = "interval") -> list[dict[str, Any]]:
        """Run all schedules due for this minute through their runners."""
        if self.state != "ready":
            self.runtime._last_schedule_check = {
                "checked_at": _utc_iso(),
                "local_time": _local_datetime(now).isoformat(),
                "trigger": trigger,
                "phase": self.state,
                "schedule_count": len(self.runtime.schedules()),
                "due_schedule_ids": [],
                "attempted_count": 0,
                "executed_count": 0,
                "failed_count": 0,
                "scheduled_jobs": len(self.runtime._schedule_unsubs),
            }
            return []
        local_now = _local_datetime(now)
        due: list[tuple[HomeiiScheduleRunner, datetime]] = []
        for runner in list(self._runners.values()):
            due_at = _due_schedule_datetime(runner.schedule, local_now)
            if due_at is None:
                continue
            run_key = self.runtime._schedule_run_key(runner.schedule, due_at)
            if self.runtime._last_schedule_runs.get(runner.key) == run_key:
                continue
            due.append((runner, due_at))
        results = [await runner.async_fire(due_at, trigger=trigger) for runner, due_at in due]
        self.runtime._last_schedule_check = {
            "checked_at": _utc_iso(),
            "local_time": local_now.isoformat(),
            "local_weekday": _homeii_weekday(local_now),
            "trigger": trigger,
            "schedule_count": len(self.runtime.schedules()),
            "due_schedule_ids": [runner.schedule_id for runner, _due_at in due if runner.schedule_id],
            "attempted_count": len(results),
            "executed_count": len([result for result in results if result.get("ok")]),
            "failed_count": len([result for result in results if not result.get("ok")]),
            "scheduled_jobs": len(self.runtime._schedule_unsubs),
        }
        self.write_status()
        return results

    async def async_run_schedule(
        self,
        profile_id: str,
        schedule_id: str,
        due_at: datetime | None = None,
        *,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        """Run a single schedule by id through its runner."""
        clean_profile = str(profile_id or DEFAULT_PROFILE_ID)
        clean_schedule_id = str(schedule_id or "").strip()
        runner = next(
            (
                item
                for item in self._runners.values()
                if item.profile_id == clean_profile and item.schedule_id == clean_schedule_id
            ),
            None,
        )
        if runner is None:
            schedule = next(
                (
                    item
                    for item in self.runtime.schedules(clean_profile)
                    if str(item.get("id") or item.get("schedule_id") or "").strip() == clean_schedule_id
                ),
                None,
            )
            if schedule is None:
                result = {
                    "ok": False,
                    "schedule_id": clean_schedule_id,
                    "profile_id": clean_profile,
                    "phase": "lookup",
                    "trigger": trigger,
                    "error": "schedule not found",
                    "executed_at": _utc_iso(),
                }
                self.runtime._last_schedule_action = result
                return result
            runner = HomeiiScheduleRunner(self, schedule)
            self._runners[runner.key] = runner
        return await runner.async_fire(due_at or _local_datetime(), trigger=trigger)

    def write_status(self) -> None:
        """Publish runner details back to runtime status fields."""
        details = [runner.details() for runner in self._runners.values()]
        self.runtime._schedule_job_details = {runner.key: runner.details() for runner in self._runners.values()}
        self.runtime._schedule_manager_status = {
            "state": self.state,
            "started_at": self.started_at,
            "ready_at": self.ready_at,
            "runner_count": len(self._runners),
            "active_timers": len(self.runtime._schedule_unsubs),
            "runners": details,
        }


class HomeiiFlowRuntime:
    """Shared runtime used by WebSocket commands and services."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize runtime."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._media_cache_store: Store[dict[str, Any]] = Store(
            hass,
            MEDIA_CACHE_STORAGE_VERSION,
            MEDIA_CACHE_STORAGE_KEY,
        )
        self._entries: dict[str, EngineEntry] = {}
        self._storage: dict[str, Any] = {
            "schedules": [],
            "timers": [],
            "volume_rules": [],
            "announcements": [],
            "activity": [],
            "playback_stats": {},
            "screensaver": {},
        }
        self._stats_cache: dict[str, Any] | None = None
        self._stats_cache_at = 0.0
        self._orchestration_unsub: Callable[[], None] | None = None
        self._minute_unsub: Callable[[], None] | None = None
        self._delayed_tick_unsubs: list[Callable[[], None]] = []
        self._last_start_at = ""
        self._last_schedule_runs: dict[str, str] = {}
        self._last_tick_at = ""
        self._last_tick_trigger = ""
        self._last_reschedule_at = ""
        self._last_schedule_check: dict[str, Any] | None = None
        self._last_schedule_action: dict[str, Any] | None = None
        self._last_timer_action: dict[str, Any] | None = None
        self._timer_execution_tasks: dict[tuple[str, str, str], asyncio.Task] = {}
        self._last_volume_action: dict[str, Any] | None = None
        self._last_announcement_action: dict[str, Any] | None = None
        self._last_button_action: dict[str, Any] | None = None
        self._schedule_unsubs: dict[str, Callable[[], None]] = {}
        self._schedule_job_details: dict[str, dict[str, Any]] = {}
        self._schedule_manager_status: dict[str, Any] = {}
        self._schedule_manager = HomeiiScheduleManager(self)
        self._playback_stats_last_tick_at: datetime | None = None
        self._playback_stats_last_save_at = 0.0
        self._playback_stats_dirty = False
        self._playback_stats_active_entities: set[str] = set()
        self._playback_stats_is_syncing = False
        self._artwork_sources: dict[str, tuple[str, float]] = {}
        self._artwork_source_tokens: dict[str, str] = {}
        self._artwork_content_cache: dict[str, tuple[float, bytes, str]] = {}
        self._library_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._library_inflight: dict[tuple[Any, ...], asyncio.Task[dict[str, Any]]] = {}
        self._queue_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._queue_inflight: dict[tuple[Any, ...], asyncio.Task[dict[str, Any]]] = {}
        self._media_command_cache: dict[str, dict[str, Any]] = {}
        self._media_command_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._media_cache_save_unsub: Callable[[], None] | None = None
        self._media_cache_warm_unsub: Callable[[], None] | None = None
        self._media_cache_warm_task: asyncio.Task[None] | None = None
        self._media_cache_metrics: dict[str, Any] = {
            "memory_hits": 0,
            "persistent_hits": 0,
            "stale_hits": 0,
            "misses": 0,
            "coalesced": 0,
            "background_refreshes": 0,
            "refresh_failures": 0,
            "last_fetch_ms": 0.0,
            "last_fetch_at": "",
            "last_persist_at": "",
        }
        self._search_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._provider_ids_cache: tuple[float, list[str]] = (0.0, [])
        self._ma_http_health: dict[str, Any] = {
            "connected": False,
            "authenticated": False,
            "last_success_at": "",
            "last_error": "",
            "last_command": "",
        }
        self._ma_preferred_base_url = ""
        self._ma_players_by_entity: dict[str, dict[str, Any]] = {}
        self._ma_players_by_id: dict[str, dict[str, Any]] = {}
        self._ma_health_probe_task: asyncio.Task[None] | None = None
        self._ma_contract_probe_at = 0.0
        self._ma_contract_checks: dict[str, dict[str, Any]] = {}
        self._snapshot_revisions: dict[str, int] = {
            "players": 1,
            "queue": 1,
            "library": 1,
        }
        self._snapshot_epoch = uuid4().hex
        self._snapshot_revision_reasons: dict[str, str] = {}
        self._music_assistant_client = MusicAssistantEventClient(
            async_get_clientsession(hass),
            self._handle_music_assistant_message,
        )

    def _bump_snapshot_revision(self, *domains: str, reason: str = "") -> None:
        """Advance state revisions after a Music Assistant event or mutation."""
        for domain in domains:
            clean_domain = _clean_string(domain).lower()
            if clean_domain not in self._snapshot_revisions:
                continue
            self._snapshot_revisions[clean_domain] += 1
            if reason:
                self._snapshot_revision_reasons[clean_domain] = reason[:160]

    def _snapshot_meta(
        self,
        domain: str,
        *,
        identity: str = "",
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Return ordering metadata used by the card to reject stale responses."""
        clean_domain = _clean_string(domain).lower()
        return {
            "domain": clean_domain,
            "epoch": self._snapshot_epoch,
            "identity": _clean_string(identity),
            "revision": int(
                revision
                if revision is not None
                else self._snapshot_revisions.get(clean_domain, 0)
            ),
            "generated_at": _utc_iso(),
            "reason": self._snapshot_revision_reasons.get(clean_domain, ""),
        }

    def _queue_result_with_snapshot(
        self,
        result: dict[str, Any],
        *,
        entity_id: str,
        queue_id: str,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Attach one atomic queue revision to every queue response shape."""
        identity = _clean_string(queue_id) or _clean_string(entity_id)
        snapshot = self._snapshot_meta("queue", identity=identity, revision=revision)
        response = dict(result)
        response["snapshot"] = snapshot
        normalized = response.get("normalized")
        if isinstance(normalized, dict):
            response["normalized"] = {**normalized, "snapshot": snapshot}
        return response

    def _cache_queue_result(
        self,
        cache_key: tuple[Any, ...],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep a very short last-good queue snapshot for instant repeated opens."""
        visible_items = int(result.get("coverage", {}).get("visible_items") or 0)
        if visible_items:
            self._queue_cache[cache_key] = {
                "stored_at": time.monotonic(),
                "revision": int(result.get("snapshot", {}).get("revision") or 0),
                "result": copy.deepcopy(result),
            }
            if len(self._queue_cache) > 24:
                oldest_key = min(
                    self._queue_cache,
                    key=lambda key: float(self._queue_cache[key].get("stored_at") or 0),
                )
                self._queue_cache.pop(oldest_key, None)
        return result

    def register_artwork_source(self, source: Any) -> str:
        """Register an artwork source and return an opaque, HA-local URL."""
        clean = str(source or "").strip()
        if not clean or clean.startswith(("data:", "blob:")):
            return ""
        if clean.startswith("/api/homeii_flow/artwork/item/"):
            return clean
        token = self._artwork_source_tokens.get(clean, "")
        if not token or token not in self._artwork_sources:
            token = hashlib.blake2s(clean.encode("utf-8"), digest_size=18).hexdigest()
            self._artwork_source_tokens[clean] = token
        self._artwork_sources[token] = (clean, time.monotonic() + 7 * 24 * 60 * 60)
        if len(self._artwork_sources) > 5000:
            now = time.monotonic()
            expired = [key for key, (_, expires_at) in self._artwork_sources.items() if expires_at <= now]
            for key in expired:
                expired_source = self._artwork_sources.pop(key, ("", 0))[0]
                if expired_source and self._artwork_source_tokens.get(expired_source) == key:
                    self._artwork_source_tokens.pop(expired_source, None)
            while len(self._artwork_sources) > 5000:
                oldest = next(iter(self._artwork_sources))
                oldest_source = self._artwork_sources.pop(oldest)[0]
                if self._artwork_source_tokens.get(oldest_source) == oldest:
                    self._artwork_source_tokens.pop(oldest_source, None)
        return f"/api/homeii_flow/artwork/item/{token}"

    def resolve_artwork_source(self, token: str) -> str:
        """Resolve a previously registered artwork token."""
        clean_token = str(token or "").strip()
        entry = self._artwork_sources.get(clean_token)
        if not entry:
            return ""
        source, expires_at = entry
        if expires_at <= time.monotonic():
            self._artwork_sources.pop(clean_token, None)
            if self._artwork_source_tokens.get(source) == clean_token:
                self._artwork_source_tokens.pop(source, None)
            return ""
        self._artwork_sources[clean_token] = (source, time.monotonic() + 7 * 24 * 60 * 60)
        return source

    def cached_artwork_content(self, source: str) -> tuple[bytes, str] | None:
        """Return recently fetched artwork without another MA round trip."""
        clean = str(source or "").strip()
        entry = self._artwork_content_cache.get(clean)
        if not entry:
            return None
        expires_at, body, content_type = entry
        if expires_at <= time.monotonic():
            self._artwork_content_cache.pop(clean, None)
            return None
        return body, content_type

    def cache_artwork_content(self, source: str, body: bytes, content_type: str) -> None:
        """Keep a bounded in-memory cache for queue and library artwork."""
        clean = str(source or "").strip()
        if not clean or not body or len(body) > 5 * 1024 * 1024:
            return
        now = time.monotonic()
        self._artwork_content_cache[clean] = (
            now + 30 * 60,
            body,
            str(content_type or "image/jpeg").split(";", 1)[0],
        )
        expired = [
            key
            for key, (expires_at, _, _) in self._artwork_content_cache.items()
            if expires_at <= now
        ]
        for key in expired:
            self._artwork_content_cache.pop(key, None)
        while len(self._artwork_content_cache) > 192:
            self._artwork_content_cache.pop(next(iter(self._artwork_content_cache)))

    @staticmethod
    def _looks_like_artwork_source(value: Any, *, key: str = "") -> bool:
        """Return whether a value looks like artwork metadata."""
        clean = _clean_string(value)
        if not clean:
            return False
        lower = clean.lower()
        key_lower = key.lower()
        if lower.startswith(("http://", "https://", "/", "imageproxy")):
            return True
        if len(clean) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in clean):
            return "proxy" in key_lower or "image" in key_lower or "art" in key_lower or "thumb" in key_lower
        if lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")):
            return True
        return "imageproxy" in lower or "media_player_proxy" in lower

    def _normalize_artwork_source(self, value: Any, *, key: str = "", provider: Any = "") -> str:
        """Return a Music Assistant/Home Assistant artwork source path."""
        clean = _clean_string(value)
        if not clean or clean.startswith(("data:", "blob:")):
            return ""
        if clean.startswith("/api/homeii_flow/artwork/"):
            return ""
        key_lower = key.lower()
        if len(clean) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in clean):
            if "proxy" in key_lower or "image" in key_lower or "art" in key_lower or "thumb" in key_lower:
                return f"/imageproxy/{clean}"
        if clean.startswith("imageproxy"):
            return f"/{clean}"
        if clean.startswith(("/imageproxy", "/api/media_player_proxy", "http://", "https://")):
            return clean
        if provider:
            return f"/imageproxy?path={quote(clean, safe='')}&provider={quote(_clean_string(provider), safe='')}&size=512"
        if self._looks_like_artwork_source(clean, key=key):
            return clean
        return ""

    def _artwork_source_candidates(self, value: Any, *, depth: int = 0, key_hint: str = "") -> list[str]:
        """Return artwork source candidates found anywhere in a media payload."""
        if value is None or depth > 8:
            return []
        candidates: list[str] = []
        if isinstance(value, str):
            source = self._normalize_artwork_source(value, key=key_hint)
            if source:
                _append_unique(candidates, source)
            return candidates
        if isinstance(value, list):
            for item in value:
                for source in self._artwork_source_candidates(item, depth=depth + 1, key_hint=key_hint):
                    _append_unique(candidates, source)
            return candidates
        if not isinstance(value, dict):
            return []

        provider = _dict_first(value, "provider", "provider_instance", "provider_domain", "provider_id")
        image_type = _clean_string(value.get("type")).lower()
        image_hint = str(key_hint or "").lower()
        path_is_image = bool(
            value.get("path")
            and (
                any(token in image_hint for token in ("image", "art", "thumb", "cover", "picture"))
                or image_type in {"thumb", "thumbnail", "fanart", "logo", "banner", "landscape", "clearart"}
            )
        )
        proxy_value = _dict_first(value, "proxy_id", "image_proxy_id", "media_image_proxy_id")
        if not proxy_value and path_is_image:
            proxy_value = value.get("path")
        if proxy_value:
            source = self._normalize_artwork_source(proxy_value, key="image_proxy_id", provider=provider)
            if source:
                _append_unique(candidates, source)

        artwork_keys = (
            "image",
            "image_url",
            "media_image",
            "media_image_url",
            "entity_picture",
            "thumbnail",
            "thumb",
            "cover",
            "cover_image",
            "cover_url",
            "artwork",
            "artwork_url",
            "picture",
            "fanart",
            "logo",
            "icon",
        )
        for key in artwork_keys:
            candidate = value.get(key)
            if isinstance(candidate, (dict, list)):
                for source in self._artwork_source_candidates(
                    candidate,
                    depth=depth + 1,
                    key_hint=key,
                ):
                    _append_unique(candidates, source)
                continue
            source = self._normalize_artwork_source(candidate, key=key, provider=provider)
            if source:
                _append_unique(candidates, source)

        for list_key in ("images", "image_items", "artworks", "thumbnails", "pictures"):
            images = value.get(list_key)
            if isinstance(images, list):
                for image in images:
                    for source in self._artwork_source_candidates(image, depth=depth + 1, key_hint=list_key):
                        _append_unique(candidates, source)

        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        if metadata:
            for key in artwork_keys:
                source = self._normalize_artwork_source(metadata.get(key), key=key, provider=provider)
                if source:
                    _append_unique(candidates, source)
            images = metadata.get("images")
            if isinstance(images, list):
                for image in images:
                    for source in self._artwork_source_candidates(image, depth=depth + 1, key_hint="image"):
                        _append_unique(candidates, source)

        album = value.get("album") if isinstance(value.get("album"), dict) else {}
        if album:
            for source in self._artwork_source_candidates(album, depth=depth + 1, key_hint="album_image"):
                _append_unique(candidates, source)

        media_item = value.get("media_item") if isinstance(value.get("media_item"), dict) else {}
        if media_item:
            for source in self._artwork_source_candidates(media_item, depth=depth + 1, key_hint="media_item_image"):
                _append_unique(candidates, source)

        return candidates

    def _first_artwork_source(self, *values: Any) -> str:
        """Return the first artwork source from candidate payloads."""
        for value in values:
            candidates = self._artwork_source_candidates(value)
            if candidates:
                return candidates[0]
        return ""

    def _proxied_artwork_url(self, *values: Any) -> str:
        """Return a HA-local artwork URL for the first candidate source."""
        source = self._first_artwork_source(*values)
        return self.register_artwork_source(source) if source else ""

    @staticmethod
    def _media_artists_text(item: dict[str, Any]) -> str:
        """Return a compact artist string from common Music Assistant payloads."""
        artists = item.get("artists")
        if isinstance(artists, list):
            names = [
                _clean_string(artist.get("name") if isinstance(artist, dict) else artist)
                for artist in artists
            ]
            names = [name for name in names if name]
            if names:
                return ", ".join(names)
        return _clean_string(
            _dict_first(
                item,
                "artist",
                "artists_str",
                "media_artist",
                "album_artist",
                "media_album_artist",
            )
        )

    def normalize_media_item(
        self,
        item: Any,
        *,
        fallback_media_type: str = "track",
        fallback_index: int = 0,
        player: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return a card-ready media item with stable ids, text and artwork."""
        if not isinstance(item, dict):
            return None
        media_item = item.get("media_item") if isinstance(item.get("media_item"), dict) else {}
        core = media_item or item
        album = core.get("album") if isinstance(core.get("album"), dict) else (
            item.get("album") if isinstance(item.get("album"), dict) else {}
        )
        album_text = _clean_string(
            core.get("album") if not isinstance(core.get("album"), (dict, list)) else ""
        ) or _clean_string(
            item.get("album") if not isinstance(item.get("album"), (dict, list)) else ""
        )
        media_type = _clean_string(
            _dict_first(core, "media_type", "media_content_type", "type")
            or _dict_first(item, "media_type", "media_content_type", "type")
            or fallback_media_type
            or "track"
        ).lower()
        uri = _clean_string(
            _dict_first(core, "uri", "media_id", "media_content_id", "item_id", "id")
            or _dict_first(item, "uri", "media_id", "media_content_id", "item_id", "id")
        )
        name = _clean_string(
            _dict_first(core, "name", "title", "media_title", "label")
            or _dict_first(item, "name", "title", "media_title", "label")
            or uri
        )
        artist = self._media_artists_text(core) or self._media_artists_text(item)
        album_name = _clean_string(
            _dict_first(album, "name", "title")
            or _dict_first(core, "album_name", "media_album_name")
            or _dict_first(item, "album_name", "media_album_name")
            or album_text
        )
        duration = _maybe_number(
            _dict_first(item, "duration", "media_duration")
            or _dict_first(core, "duration", "media_duration")
        )
        artwork_url = self._proxied_artwork_url(item, core, album)
        queue_item_id = _clean_string(
            _dict_first(item, "queue_item_id", "queue_service_id", "item_id", "id")
            or uri
            or f"item_{fallback_index}"
        )
        normalized = {
            **item,
            "id": _clean_string(_dict_first(core, "id", "item_id") or _dict_first(item, "id", "item_id") or uri or queue_item_id),
            "uri": uri,
            "media_type": media_type,
            "type": media_type,
            "name": name,
            "title": name,
            "artist": artist,
            "album_name": album_name,
            "duration": duration,
            "sort_index": fallback_index,
            "queue_item_id": queue_item_id,
            "queue_service_id": queue_item_id,
            "provider": _clean_string(_dict_first(core, "provider", "provider_instance", "provider_domain") or _dict_first(item, "provider", "provider_instance", "provider_domain")),
            "provider_label": _clean_string(_dict_first(core, "provider_label", "provider_name") or _dict_first(item, "provider_label", "provider_name")),
            "favorite": bool(_dict_first(core, "favorite", "in_library", "library") or _dict_first(item, "favorite", "in_library", "library")),
            "resume_position_ms": _dict_first(
                core,
                "resume_position_ms",
                "position_ms",
                "progress_ms",
            )
            or _dict_first(item, "resume_position_ms", "position_ms", "progress_ms"),
            "fully_played": bool(
                _dict_first(core, "fully_played", "played")
                or _dict_first(item, "fully_played", "played")
            ),
            "chapter": _dict_first(core, "chapter", "chapter_number")
            or _dict_first(item, "chapter", "chapter_number"),
            "explicit": bool(
                _dict_first(core, "explicit")
                or _dict_first(item, "explicit")
            ),
            "homeii_artwork_url": artwork_url,
            "image": artwork_url,
            "image_url": artwork_url,
        }
        normalized["media_item"] = {
            **media_item,
            "uri": uri,
            "media_type": media_type,
            "type": media_type,
            "name": name,
            "title": name,
            "artist": artist,
            "album_name": album_name,
            "album": album or media_item.get("album") or item.get("album"),
            "duration": duration,
            "homeii_artwork_url": artwork_url,
            "image": artwork_url,
            "image_url": artwork_url,
        }
        return normalized

    def decorate_artwork_urls(self, value: Any, *, depth: int = 0) -> Any:
        """Attach HA-local artwork URLs to queue/library response dictionaries."""
        if value is None or depth > 10:
            return value
        if isinstance(value, list):
            for item in value:
                self.decorate_artwork_urls(item, depth=depth + 1)
            return value
        if not isinstance(value, dict):
            return value
        existing_artwork_url = _clean_string(value.get("homeii_artwork_url"))
        artwork_url = (
            existing_artwork_url
            if existing_artwork_url.startswith("/api/homeii_flow/artwork/item/")
            else self._proxied_artwork_url(value)
        )
        if artwork_url:
            value["homeii_artwork_url"] = artwork_url
        for child in value.values():
            if isinstance(child, (dict, list)):
                self.decorate_artwork_urls(child, depth=depth + 1)
        return value

    @staticmethod
    def _library_cache_item_count(result: dict[str, Any]) -> int:
        """Return the number of normalized media items in a cached result."""
        return len(result.get("items", [])) if isinstance(result.get("items"), list) else 0

    def _persistent_cache_result(
        self,
        result: dict[str, Any],
        *,
        compact_library: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Return a compact cache payload plus artwork source mappings."""
        compact = {
            key: copy.deepcopy(value)
            for key, value in result.items()
            if not compact_library or key not in {"data", "attempts", "cache"}
        }
        artwork_sources: dict[str, str] = {}

        def collect(value: Any) -> None:
            if isinstance(value, str) and value.startswith("/api/homeii_flow/artwork/item/"):
                token = value.rsplit("/", 1)[-1]
                source = self.resolve_artwork_source(token)
                if source:
                    artwork_sources[token] = source
                return
            if isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)

        collect(compact)
        return compact, artwork_sources

    def _restore_persistent_cache_result(
        self,
        result: dict[str, Any],
        artwork_sources: dict[str, Any],
    ) -> dict[str, Any]:
        """Refresh persisted opaque artwork URLs for the current runtime."""
        replacements = {
            str(token): self.register_artwork_source(source)
            for token, source in artwork_sources.items()
            if str(token).strip() and str(source or "").strip()
        }

        def replace(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("/api/homeii_flow/artwork/item/"):
                token = value.rsplit("/", 1)[-1]
                return replacements.get(token) or ""
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        return replace(copy.deepcopy(result))

    async def _async_load_media_cache(self) -> None:
        """Load bounded library snapshots for instant startup browsing."""
        try:
            stored = await self._media_cache_store.async_load()
        except Exception:  # noqa: BLE001 - cache corruption must not block integration setup
            _LOGGER.warning("Could not load HOMEii media cache; starting with an empty cache")
            self._media_cache_metrics["load_failures"] = int(
                self._media_cache_metrics.get("load_failures") or 0
            ) + 1
            return
        if not isinstance(stored, dict) or int(stored.get("version") or 0) != MEDIA_CACHE_STORAGE_VERSION:
            self._media_cache_metrics["schema_resets"] = int(
                self._media_cache_metrics.get("schema_resets") or 0
            ) + 1
            return
        entries = stored.get("entries", []) if isinstance(stored, dict) else []
        command_entries = stored.get("command_entries", []) if isinstance(stored, dict) else []
        if not isinstance(entries, list):
            entries = []
        if not isinstance(command_entries, list):
            command_entries = []
        now_epoch = time.time()
        now_mono = time.monotonic()
        restored_count = 0
        for entry in entries[:80]:
            if not isinstance(entry, dict):
                continue
            key_parts = entry.get("key")
            result = entry.get("result")
            try:
                stored_at = float(entry.get("stored_at") or 0)
            except (TypeError, ValueError):
                continue
            age = now_epoch - stored_at
            if not isinstance(key_parts, list) or not isinstance(result, dict) or age < 0 or age > 24 * 60 * 60:
                continue
            key = tuple(key_parts)
            restored = self._restore_persistent_cache_result(
                result,
                entry.get("artwork_sources") if isinstance(entry.get("artwork_sources"), dict) else {},
            )
            self._library_cache[key] = {
                "fresh_until": 0.0 if entry.get("invalidated") else now_mono + max(0.0, 10 * 60 - age),
                "stale_until": now_mono + max(30.0, 24 * 60 * 60 - age),
                "stored_at": stored_at,
                "result": restored,
                "persistent": True,
                "invalidated": bool(entry.get("invalidated")),
            }
            restored_count += 1
        command_restored_count = 0
        for entry in command_entries[:40]:
            if not isinstance(entry, dict):
                continue
            key = _clean_string(entry.get("key"))
            result = entry.get("result")
            try:
                stored_at = float(entry.get("stored_at") or 0)
            except (TypeError, ValueError):
                continue
            age = now_epoch - stored_at
            if not key or not isinstance(result, dict) or age < 0 or age > 24 * 60 * 60:
                continue
            restored = self._restore_persistent_cache_result(
                result,
                entry.get("artwork_sources") if isinstance(entry.get("artwork_sources"), dict) else {},
            )
            self._media_command_cache[key] = {
                "fresh_until": 0.0 if entry.get("invalidated") else now_mono + max(0.0, 15 * 60 - age),
                "stale_until": now_mono + max(30.0, 24 * 60 * 60 - age),
                "stored_at": stored_at,
                "result": restored,
                "persistent": True,
                "invalidated": bool(entry.get("invalidated")),
            }
            command_restored_count += 1
        if restored_count:
            self._media_cache_metrics["persistent_entries_loaded"] = restored_count
        if command_restored_count:
            self._media_cache_metrics["persistent_command_entries_loaded"] = command_restored_count

    def _schedule_media_cache_save(self) -> None:
        """Debounce persistent cache writes away from foreground requests."""
        if self._media_cache_save_unsub is not None:
            return

        @callback
        def save_later(_: datetime) -> None:
            self._media_cache_save_unsub = None
            self.hass.async_create_task(self._async_save_media_cache())

        self._media_cache_save_unsub = async_call_later(self.hass, 2, save_later)

    async def _async_save_media_cache(self) -> None:
        """Persist the most useful bounded media snapshots."""
        candidates = sorted(
            self._library_cache.items(),
            key=lambda item: float(item[1].get("stored_at") or 0),
            reverse=True,
        )
        entries: list[dict[str, Any]] = []
        total_items = 0
        for key, cached in candidates:
            result = cached.get("result")
            if not isinstance(result, dict):
                continue
            item_count = self._library_cache_item_count(result)
            if entries and (len(entries) >= 48 or total_items + item_count > 8000):
                continue
            compact, artwork_sources = self._persistent_cache_result(result)
            entries.append(
                {
                    "key": list(key),
                    "stored_at": float(cached.get("stored_at") or time.time()),
                    "invalidated": bool(cached.get("invalidated")),
                    "result": compact,
                    "artwork_sources": artwork_sources,
                }
            )
            total_items += item_count
        command_entries: list[dict[str, Any]] = []
        for key, cached in sorted(
            self._media_command_cache.items(),
            key=lambda item: float(item[1].get("stored_at") or 0),
            reverse=True,
        )[:32]:
            result = cached.get("result")
            if not isinstance(result, dict):
                continue
            compact, artwork_sources = self._persistent_cache_result(
                result,
                compact_library=False,
            )
            command_entries.append(
                {
                    "key": key,
                    "stored_at": float(cached.get("stored_at") or time.time()),
                    "invalidated": bool(cached.get("invalidated")),
                    "result": compact,
                    "artwork_sources": artwork_sources,
                }
            )
        try:
            await self._media_cache_store.async_save(
                {
                    "version": MEDIA_CACHE_STORAGE_VERSION,
                    "saved_at": _utc_iso(),
                    "entries": entries,
                    "command_entries": command_entries,
                    "items": total_items,
                }
            )
        except Exception:  # noqa: BLE001 - foreground browsing must survive storage failures
            self._media_cache_metrics["persist_failures"] = int(
                self._media_cache_metrics.get("persist_failures") or 0
            ) + 1
            _LOGGER.warning("Could not persist HOMEii media cache")
            return
        self._media_cache_metrics["last_persist_at"] = _utc_iso()
        self._media_cache_metrics["persistent_entries"] = len(entries)
        self._media_cache_metrics["persistent_command_entries"] = len(command_entries)
        self._media_cache_metrics["persistent_items"] = total_items

    def library_cache_status(self) -> dict[str, Any]:
        """Return privacy-safe cache health and performance counters."""
        now = time.monotonic()
        fresh = sum(1 for item in self._library_cache.values() if float(item.get("fresh_until") or 0) > now)
        stale = sum(
            1
            for item in self._library_cache.values()
            if float(item.get("fresh_until") or 0) <= now < float(item.get("stale_until") or 0)
        )
        return {
            **self._media_cache_metrics,
            "memory_entries": len(self._library_cache),
            "command_memory_entries": len(self._media_command_cache),
            "queue_memory_entries": len(self._queue_cache),
            "fresh_entries": fresh,
            "stale_entries": stale,
            "inflight_requests": len(self._library_inflight),
            "command_inflight_requests": len(self._media_command_inflight),
            "queue_inflight_requests": len(self._queue_inflight),
            "snapshot_epoch": self._snapshot_epoch,
            "snapshot_revisions": dict(self._snapshot_revisions),
            "cached_items": sum(
                self._library_cache_item_count(item.get("result", {}))
                for item in self._library_cache.values()
            ),
            "fresh_ttl_seconds": 600,
            "stale_ttl_seconds": 86400,
        }

    def _schedule_media_cache_warm(self) -> None:
        """Warm common library shelves after startup without delaying setup."""
        if self._media_cache_warm_unsub is not None or (
            self._media_cache_warm_task is not None and not self._media_cache_warm_task.done()
        ):
            return

        @callback
        def warm_later(_: datetime) -> None:
            self._media_cache_warm_unsub = None
            self._media_cache_warm_task = self.hass.async_create_task(self._async_warm_media_cache())

        self._media_cache_warm_unsub = async_call_later(self.hass, 3, warm_later)

    async def _async_warm_media_cache(self) -> None:
        """Populate common library views sequentially with bounded load."""
        started = time.perf_counter()
        warmed = 0
        self._media_cache_metrics["warm_status"] = "running"
        for media_type, limit in (
            ("playlist", 500),
            ("album", 250),
            ("artist", 250),
            ("radio", 250),
            ("track", 350),
            ("podcast", 250),
        ):
            try:
                await self.async_get_library(
                    {
                        "media_type": media_type,
                        "order_by": "sort_name",
                        "limit": limit,
                    }
                )
                warmed += 1
            except Exception:  # noqa: BLE001 - warm-up is best effort
                self._media_cache_metrics["warm_failures"] = int(
                    self._media_cache_metrics.get("warm_failures") or 0
                ) + 1
            await asyncio.sleep(0)
        self._media_cache_metrics["warm_status"] = "ready" if warmed else "degraded"
        self._media_cache_metrics["warm_shelves"] = warmed
        self._media_cache_metrics["last_warm_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self._media_cache_metrics["last_warm_at"] = _utc_iso()

    async def async_load(self) -> None:
        """Load stored Engine data."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._storage.update(
                {
                    "schedules": _safe_list(stored.get("schedules")),
                    "timers": _safe_list(stored.get("timers")),
                    "volume_rules": _safe_list(stored.get("volume_rules")),
                    "announcements": _safe_list(stored.get("announcements")),
                    "activity": _safe_list(stored.get("activity")),
                    "playback_stats": stored.get("playback_stats") if isinstance(stored.get("playback_stats"), dict) else {},
                    "screensaver": stored.get("screensaver") if isinstance(stored.get("screensaver"), dict) else {},
                }
            )
        await self._async_load_media_cache()

    def async_start_orchestration(self) -> None:
        """Start lightweight schedule and policy enforcement."""
        if self._orchestration_unsub is not None:
            self._reschedule_all_schedules()
            self._schedule_background_tick(0, "restart")
            return

        @callback
        def tick(now: datetime) -> None:
            self.hass.async_create_task(self.async_tick_orchestration(now, trigger="interval"))

        @callback
        def minute_tick(now: datetime) -> None:
            self.hass.async_create_task(self.async_tick_orchestration(now, trigger="minute"))

        self._last_start_at = _local_datetime().isoformat()
        self._orchestration_unsub = async_track_time_interval(self.hass, tick, timedelta(seconds=30))
        self._minute_unsub = async_track_time_change(self.hass, minute_tick, second=0)
        self._schedule_manager.start()
        self._schedule_background_tick(0, "startup")
        self._schedule_background_tick(5, "startup_delayed")
        self._schedule_background_tick(20, "startup_probe")

    def async_stop_orchestration(self) -> None:
        """Stop schedule and policy enforcement."""
        if self._orchestration_unsub is None:
            return
        self._orchestration_unsub()
        self._orchestration_unsub = None
        if self._minute_unsub is not None:
            self._minute_unsub()
            self._minute_unsub = None
        for unsubscribe in list(self._delayed_tick_unsubs):
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001 - best effort cleanup
                continue
        self._delayed_tick_unsubs = []
        self._schedule_manager.stop()

    def _schedule_background_tick(self, delay: int | float, trigger: str) -> None:
        """Schedule one orchestration pass without depending on the card being open."""
        if delay <= 0:
            self.hass.async_create_task(self.async_tick_orchestration(trigger=trigger))
            return

        @callback
        def run_once(now: datetime) -> None:
            self.hass.async_create_task(self.async_tick_orchestration(now, trigger=trigger))

        self._delayed_tick_unsubs.append(async_call_later(self.hass, delay, run_once))

    def _schedule_storage_key(self, schedule: dict[str, Any]) -> str:
        """Return a stable schedule storage key."""
        return f"{schedule.get('profile_id') or DEFAULT_PROFILE_ID}:{schedule.get('id') or schedule.get('schedule_id') or ''}"

    def _clear_schedule_jobs(self) -> None:
        """Cancel all point-in-time schedule jobs."""
        self._schedule_manager.stop()
        self._schedule_unsubs = {}
        self._schedule_job_details = {}

    def _reschedule_all_schedules(self) -> None:
        """Register exact Home Assistant jobs for all enabled schedules."""
        self._schedule_manager.reschedule_all()

    async def async_tick_orchestration(self, now: datetime | None = None, *, trigger: str = "manual") -> dict[str, Any]:
        """Run one orchestration pass."""
        local_now = _local_datetime(now)
        self._last_tick_at = local_now.isoformat()
        self._last_tick_trigger = trigger
        self._last_schedule_check = {
            "checked_at": _utc_iso(),
            "local_time": local_now.isoformat(),
            "local_weekday": _homeii_weekday(local_now),
            "trigger": trigger,
            "phase": "running",
            "schedule_count": len(self.schedules()),
            "scheduled_jobs": len(self._schedule_unsubs),
        }
        await self.async_update_playback_statistics(local_now)
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        schedule_results = await self.async_run_due_schedules(local_now)
        timer_results = await self.async_run_due_timers(local_now)
        volume_results = await self.async_enforce_volume_rules(local_now)
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        return {
            "generated_at": _utc_iso(),
            "local_time": self._last_tick_at,
            "schedules": schedule_results,
            "timers": timer_results,
            "volume_rules": volume_results,
        }

    def orchestration_status(self) -> dict[str, Any]:
        """Return the current orchestration runtime status."""
        return {
            "running": self._orchestration_unsub is not None,
            "minute_watcher": self._minute_unsub is not None,
            "interval_seconds": 30,
            "started_at": self._last_start_at,
            "last_tick_at": self._last_tick_at,
            "last_tick_trigger": self._last_tick_trigger,
            "last_reschedule_at": self._last_reschedule_at,
            "last_schedule_check": self._last_schedule_check,
            "last_schedule_action": self._last_schedule_action,
            "last_timer_action": self._last_timer_action,
            "last_volume_action": self._last_volume_action,
            "last_announcement_action": self._last_announcement_action,
            "last_button_action": self._last_button_action,
            "known_schedule_run_keys": len(self._last_schedule_runs),
            "scheduled_jobs": len(self._schedule_unsubs),
            "scheduled_job_keys": list(self._schedule_unsubs.keys()),
            "scheduled_job_details": list(self._schedule_job_details.values()),
            "schedule_manager": self._schedule_manager_status,
        }

    async def async_save(self) -> None:
        """Persist Engine data."""
        await self._store.async_save(self._storage)
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)

    def register_entry(
        self,
        entry_id: str,
        *,
        instance_id: str,
        profile_id: str,
        title: str,
        enable_experimental: bool,
        music_assistant_url: str = "",
        music_assistant_external_url: str = "",
        music_assistant_token: str = "",
    ) -> None:
        """Register a loaded config entry."""
        self._entries[entry_id] = EngineEntry(
            entry_id=entry_id,
            instance_id=instance_id or DEFAULT_INSTANCE_ID,
            profile_id=profile_id or DEFAULT_PROFILE_ID,
            title=title or "HOMEii Flow Engine",
            enable_experimental=enable_experimental,
            music_assistant_url=_normalized_http_url(music_assistant_url),
            music_assistant_external_url=_normalized_http_url(music_assistant_external_url),
            music_assistant_token=_clean_string(music_assistant_token),
        )
        self._refresh_music_assistant_connection()
        self._schedule_media_cache_warm()
        self._schedule_music_assistant_health_probe()

    def unregister_entry(self, entry_id: str) -> None:
        """Unregister a loaded config entry."""
        self._entries.pop(entry_id, None)
        if self._entries:
            self._refresh_music_assistant_connection()
        else:
            self._music_assistant_client.configure("", "")

    def _refresh_music_assistant_connection(self) -> None:
        """Apply the best available MA URL and token to the persistent event client."""
        try:
            urls = self.music_assistant_base_urls()
            tokens = self.music_assistant_tokens()
            self._music_assistant_client.configure(urls, tokens[0] if tokens else "")
        except Exception:  # noqa: BLE001 - MA reachability must never fail HA entry setup
            _LOGGER.exception("Could not initialize the Music Assistant event connection")

    def _schedule_music_assistant_health_probe(self) -> None:
        """Start one coalesced MA health/player probe after entry setup."""
        if self._ma_health_probe_task and not self._ma_health_probe_task.done():
            return

        async def probe() -> None:
            try:
                await self._async_music_assistant_server_info()
                player_snapshot = await self.async_players_snapshot()
                await self._async_music_assistant_contract_probe(player_snapshot, force=True)
            except Exception as err:  # noqa: BLE001 - diagnostics expose startup failures
                _LOGGER.warning("HOMEii Flow Engine Music Assistant probe failed: %s", err)
            finally:
                async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)

        self._ma_health_probe_task = self.hass.async_create_task(probe())

    def _mark_library_cache_stale(self, media_types: set[str] | None = None) -> None:
        """Expire matching cache entries without discarding usable snapshots."""
        normalized_types = {item.lower() for item in (media_types or set()) if item}
        for key, cached in self._library_cache.items():
            media_type = str(key[0] if key else "").lower()
            if normalized_types and media_type not in normalized_types:
                continue
            cached["fresh_until"] = 0.0
            cached["invalidated"] = True
        self._schedule_media_cache_save()

    def _mark_media_command_cache_stale(self) -> None:
        """Expire cached media details while retaining an instant stale snapshot."""
        for cached in self._media_command_cache.values():
            cached["fresh_until"] = 0.0
            cached["invalidated"] = True
        self._schedule_media_cache_save()

    def _handle_music_assistant_message(self, message: dict[str, Any]) -> None:
        """Invalidate backend caches and forward a compact event through HA."""
        progress_only = False
        if message.get("kind") == "event":
            event_name = _clean_string(message.get("event")).lower()
            progress_only = any(
                token in event_name
                for token in (
                    "elapsed",
                    "progress",
                    "position",
                    "time_updated",
                    "queue_time",
                    "player_time",
                )
            )
            playback_event = any(
                token in event_name
                for token in ("queue", "player", "playback", "media_item_played", "elapsed", "progress", "position")
            )
            affects_library = not playback_event and (
                not event_name
                or any(
                    token in event_name
                    for token in (
                        "library",
                        "favorite",
                        "provider",
                        "sync",
                        "playlist",
                        "album",
                        "artist",
                        "track",
                        "podcast",
                        "audiobook",
                        "radio",
                        "genre",
                    )
                )
            )
            if affects_library and not progress_only:
                self._bump_snapshot_revision("library", reason=event_name or "music_assistant_event")
                affected_types = {
                    media_type
                    for media_type in ("playlist", "album", "artist", "track", "podcast", "audiobook", "radio", "genre")
                    if media_type in event_name
                }
                self._mark_library_cache_stale(affected_types or None)
                self._mark_media_command_cache_stale()
                self._search_cache.clear()
                if not event_name or "provider" in event_name:
                    self._provider_ids_cache = (0.0, [])
            if not progress_only and (not event_name or "player" in event_name or "queue" in event_name):
                self._stats_cache = None
            if not progress_only:
                changed_domains: list[str] = []
                if not event_name or "player" in event_name:
                    changed_domains.append("players")
                if not event_name or "queue" in event_name or "media_item_played" in event_name:
                    changed_domains.append("queue")
                if changed_domains:
                    self._bump_snapshot_revision(*changed_domains, reason=event_name or "music_assistant_event")
        self.hass.bus.async_fire(EVENT_MUSIC_ASSISTANT, dict(message))
        if not progress_only:
            async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)

    @property
    def entries(self) -> list[EngineEntry]:
        """Return loaded entries."""
        return list(self._entries.values())

    def _matching_entry(self, instance_id: str | None = None) -> EngineEntry | None:
        """Return a matching loaded entry."""
        clean_instance = str(instance_id or "").strip()
        if clean_instance:
            for entry in self._entries.values():
                if entry.instance_id == clean_instance:
                    return entry
        return next(iter(self._entries.values()), None)

    def context(
        self,
        *,
        instance_id: str | None = None,
        profile_id: str | None = None,
        required_connections: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return context consumed by the card."""
        entry = self._matching_entry(instance_id)
        resolved_instance = str(instance_id or (entry.instance_id if entry else DEFAULT_INSTANCE_ID))
        resolved_profile = str(profile_id or (entry.profile_id if entry else DEFAULT_PROFILE_ID))
        return {
            "available": True,
            "version": VERSION,
            "engine_version": VERSION,
            "instance_id": resolved_instance,
            "profile_id": resolved_profile,
            "capabilities": CAPABILITIES,
            "frontend": {
                "system_screensaver_url": "/homeii_flow/homeii-flow-system-screensaver.js",
                "brand_icon_url": "/homeii_flow/homeii-flow-icon.png",
                "brand_logo_url": "/homeii_flow/homeii-flow-logo.png",
            },
            "entries": [loaded.as_dict() for loaded in self.entries],
            "loaded_entries": len(self._entries),
            "required_connections": required_connections or self.required_connections_snapshot(),
            "music_assistant": self._music_assistant_client.snapshot(),
            "media_cache": self.library_cache_status(),
            "generated_at": _utc_iso(),
        }

    async def async_bootstrap_snapshot(
        self,
        *,
        instance_id: str | None = None,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one coherent connection and player snapshot for card startup."""
        await self._async_music_assistant_server_info()
        player_snapshot = await self.async_players_snapshot()
        await self._async_music_assistant_contract_probe(player_snapshot)
        required_connections = self.required_connections_snapshot()
        return {
            **self.context(
                instance_id=instance_id,
                profile_id=profile_id,
                required_connections=required_connections,
            ),
            "bootstrap": True,
            "player_snapshot": player_snapshot,
        }

    def services_snapshot(self) -> dict[str, Any]:
        """Return service availability information."""
        services = self.hass.services.async_services()
        return {
            "music_assistant": sorted(services.get("music_assistant", {}).keys()),
            "music_assistant_config_entries": self.music_assistant_config_entries_snapshot(),
            "music_assistant_config_entry_id": self.music_assistant_config_entry_id(),
            "media_player_join": self.hass.services.has_service("media_player", "join"),
            "media_player_unjoin": self.hass.services.has_service("media_player", "unjoin"),
            "tts_speak": self.hass.services.has_service("tts", "speak"),
            "required_connections": self.required_connections_snapshot(),
        }

    @staticmethod
    def _entry_state_value(entry: Any) -> str:
        """Return a normalized Home Assistant config-entry state string."""
        state = getattr(entry, "state", "")
        value = getattr(state, "value", None)
        return str(value if value is not None else state or "").lower()

    def music_assistant_config_entries_snapshot(self) -> list[dict[str, Any]]:
        """Return visible Music Assistant config entries."""
        try:
            entries = self.hass.config_entries.async_entries("music_assistant")
        except Exception:  # noqa: BLE001 - diagnostics should not fail setup
            return []
        snapshot: list[dict[str, Any]] = []
        for entry in entries:
            snapshot.append(
                {
                    "entry_id": getattr(entry, "entry_id", ""),
                    "title": getattr(entry, "title", ""),
                    "state": self._entry_state_value(entry),
                }
            )
        return snapshot

    def required_connections_snapshot(
        self,
        *,
        all_players: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return the required backend connections for HOMEii Flow 6."""
        services = self.hass.services.async_services()
        music_assistant_services = sorted(services.get("music_assistant", {}).keys())
        entries = self.music_assistant_config_entries_snapshot()
        loaded_entries = [
            entry
            for entry in entries
            if str(entry.get("state") or "").lower() == "loaded"
            or str(entry.get("state") or "").lower().endswith(".loaded")
        ]
        all_players = all_players if all_players is not None else self.media_players_snapshot(include_artwork=False)
        music_assistant_players = [player for player in all_players if self.is_music_assistant_player(player)]
        required_services: tuple[str, ...] = ()
        command_bridge_urls = self.music_assistant_base_urls()
        command_bridge_tokens = self.music_assistant_tokens()
        realtime = self._music_assistant_client.snapshot()
        command_bridge_configured = bool(command_bridge_urls and command_bridge_tokens)
        command_bridge_ok = bool(
            command_bridge_configured
            and self._ma_http_health.get("connected")
            and self._ma_http_health.get("authenticated")
            and int(self._ma_http_health.get("schema_version") or 0)
            >= MUSIC_ASSISTANT_SCHEMA_MIN
        )
        queue_check = self._ma_contract_checks.get("queue", {})
        library_check = self._ma_contract_checks.get("library", {})
        search_check = self._ma_contract_checks.get("search", {})
        full_queue_ok = bool(command_bridge_ok and queue_check.get("ok"))
        library_ok = bool(command_bridge_ok and library_check.get("ok"))
        search_ok = bool(command_bridge_ok and search_check.get("ok"))
        missing_services: list[str] = []
        direct_player_count = int(self._ma_http_health.get("player_count") or 0)
        ma_ok = bool(loaded_entries and command_bridge_ok and direct_player_count > 0)
        ma_status = "connected" if ma_ok else "attention" if (entries or music_assistant_services) else "missing"
        if not entries:
            ma_message = "Music Assistant config entry was not found."
        elif not loaded_entries:
            ma_message = "Music Assistant config entry exists but is not loaded."
        elif not command_bridge_ok:
            ma_message = (
                "Music Assistant is loaded, but its authenticated API "
                f"schema {MUSIC_ASSISTANT_SCHEMA_MIN}+ is not ready."
            )
        elif not direct_player_count:
            ma_message = "Music Assistant is loaded, but its API returned no enabled players."
        else:
            ma_message = f"Music Assistant is connected with {direct_player_count} API player(s)."
        connections = {
            "music_assistant": {
                "ok": ma_ok,
                "status": ma_status,
                "message": ma_message,
                "entries": entries,
                "loaded_entry_count": len(loaded_entries),
                "service_count": len(music_assistant_services),
                "services": music_assistant_services,
                "required_services": list(required_services),
                "missing_services": missing_services,
                "music_assistant_player_count": direct_player_count,
                "all_media_player_count": len(all_players),
                "players": [
                    {
                        "entity_id": player.get("entity_id"),
                        "friendly_name": player.get("friendly_name"),
                        "state": player.get("state"),
                        "registry_platform": player.get("registry_platform"),
                        "active_queue": player.get("active_queue"),
                    }
                    for player in music_assistant_players[:24]
                ],
            },
            "queue_provider": {
                "ok": full_queue_ok,
                "status": "connected" if full_queue_ok else "attention",
                "message": "Authenticated MA 2.10 queue API is available."
                if full_queue_ok
                else _clean_string(queue_check.get("error"))
                or "Authenticated MA 2.10 queue API is not ready.",
                "probe": queue_check,
            },
            "library_provider": {
                "ok": library_ok,
                "status": "connected" if library_ok else "missing",
                "message": "Authenticated Music Assistant library API is available."
                if library_ok
                else _clean_string(library_check.get("error"))
                or "Authenticated Music Assistant library API is not ready.",
                "probe": library_check,
            },
            "search_provider": {
                "ok": search_ok,
                "status": "connected" if search_ok else "missing",
                "message": "Authenticated Music Assistant provider search API is available."
                if search_ok
                else _clean_string(search_check.get("error"))
                or "Authenticated Music Assistant provider search API is not ready.",
                "probe": search_check,
            },
            "command_bridge": {
                "ok": command_bridge_ok,
                "status": "connected" if command_bridge_ok else "attention",
                "message": "Music Assistant 2.10 server API is authenticated."
                if command_bridge_ok
                else "Music Assistant server URL and token are required and must authenticate successfully.",
                "url_count": len(command_bridge_urls),
                "token_configured": bool(command_bridge_tokens),
                "configured": command_bridge_configured,
                **self._ma_http_health,
            },
            "realtime_events": {
                "ok": bool(realtime.get("authenticated") and realtime.get("schema_supported")),
                "status": "connected"
                if realtime.get("authenticated") and realtime.get("schema_supported")
                else "attention",
                "message": "Authenticated Music Assistant event stream is connected."
                if realtime.get("authenticated") and realtime.get("schema_supported")
                else "Music Assistant API schema is older than the supported minimum."
                if realtime.get("authenticated")
                else "Music Assistant event stream is not authenticated.",
                **realtime,
            },
        }
        required_ok = bool(
            connections["music_assistant"]["ok"]
            and connections["queue_provider"]["ok"]
            and connections["library_provider"]["ok"]
            and connections["search_provider"]["ok"]
            and connections["command_bridge"]["ok"]
        )
        status = "healthy" if required_ok else "attention"
        return {
            "status": status,
            "ok": required_ok,
            "generated_at": _utc_iso(),
            "summary": "Required connections are ready." if required_ok else "One or more required connections need attention.",
            "connections": connections,
            "music_assistant": connections["music_assistant"],
            "queue_provider": connections["queue_provider"],
            "library_provider": connections["library_provider"],
            "search_provider": connections["search_provider"],
            "command_bridge": connections["command_bridge"],
            "realtime_events": connections["realtime_events"],
        }

    def music_assistant_config_entry_id(self) -> str:
        """Return the best Music Assistant config entry id for backend service calls."""
        entries = self.music_assistant_config_entries_snapshot()
        if not entries:
            return ""

        def rank(entry: dict[str, Any]) -> int:
            state = str(entry.get("state") or "").lower()
            if state == "loaded" or state.endswith(".loaded"):
                return 0
            if "setup_retry" in state:
                return 1
            if "not_loaded" in state:
                return 2
            return 3

        preferred = sorted(entries, key=rank)[0]
        return str(preferred.get("entry_id") or "").strip()

    def music_assistant_base_urls(self) -> list[str]:
        """Return configured and auto-discovered Music Assistant base URLs."""
        urls: list[str] = []
        for loaded in self._entries.values():
            for value in (loaded.music_assistant_url, loaded.music_assistant_external_url):
                candidate = _normalized_http_url(value)
                if candidate and candidate not in urls:
                    urls.append(candidate)
        try:
            entries = self.hass.config_entries.async_entries("music_assistant")
        except Exception:  # noqa: BLE001 - diagnostics should not fail setup
            return urls
        for entry in entries:
            sources = []
            data = getattr(entry, "data", None)
            options = getattr(entry, "options", None)
            if isinstance(data, dict):
                sources.append(data)
            if isinstance(options, dict):
                sources.append(options)
            for source in sources:
                for key in (
                    "url",
                    "base_url",
                    "server_url",
                    "web_url",
                    "mass_url",
                    "music_assistant_url",
                    "music_assistant_external_url",
                    "external_url",
                    "internal_url",
                    "address",
                ):
                    raw_url = _normalized_http_url(source.get(key))
                    if raw_url and raw_url not in urls:
                        urls.append(raw_url)
                host = str(source.get("host") or source.get("hostname") or source.get("ip_address") or "").strip()
                if host:
                    if host.startswith(("http://", "https://")):
                        candidate = _normalized_http_url(host)
                    else:
                        ssl_value = source.get("ssl")
                        protocol = "https" if ssl_value is True or str(ssl_value).lower() in {"true", "1", "yes", "https"} else "http"
                        port = str(source.get("port") or source.get("webserver_port") or 8095).strip()
                        candidate = f"{protocol}://{host}{f':{port}' if port else ''}"
                    if candidate and candidate not in urls:
                        urls.append(candidate)
        return urls

    def music_assistant_tokens(self) -> list[str]:
        """Return Engine-owned Music Assistant API tokens without exposing them."""
        tokens: list[str] = []
        for loaded in self._entries.values():
            token = _clean_string(loaded.music_assistant_token)
            if token and token not in tokens:
                tokens.append(token)
        return tokens

    def _screensaver_store(self) -> dict[str, Any]:
        """Return the mutable screensaver storage bucket."""
        store = self._storage.get("screensaver")
        if not isinstance(store, dict):
            store = {}
            self._storage["screensaver"] = store
        return store

    def _screensaver_raw_config(self, profile_id: str | None = None) -> dict[str, Any]:
        """Return the raw screensaver config for a profile, including private fields."""
        clean_profile = str(profile_id or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        config = self._screensaver_store().get(clean_profile)
        return dict(config) if isinstance(config, dict) else {}

    def screensaver_music_assistant_base_urls(self, profile_id: str | None = None) -> list[str]:
        """Return Music Assistant URLs for screensaver artwork resolution."""
        config = self._screensaver_raw_config(profile_id)
        urls: list[str] = []
        for candidate in self.music_assistant_base_urls():
            clean = _normalized_http_url(candidate)
            if clean and clean not in urls:
                urls.append(clean)
        for key in ("music_assistant_url", "ma_url", "music_assistant_external_url"):
            candidate = _normalized_http_url(config.get(key))
            if candidate and candidate not in urls:
                urls.append(candidate)
        return urls

    def _music_assistant_service_payload(
        self,
        domain: str,
        service_data: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Attach the Music Assistant config entry id when the integration exposes one."""
        data = dict(service_data or {})
        if domain != "music_assistant" or data.get("config_entry_id"):
            return data, str(data.get("config_entry_id") or "")
        entry_id = self.music_assistant_config_entry_id()
        if not entry_id:
            return data, ""
        return {"config_entry_id": entry_id, **data}, entry_id

    @staticmethod
    def _service_rejected_config_entry_id(error: Exception) -> bool:
        """Return whether Home Assistant rejected config_entry_id for this service schema."""
        message = str(error).lower()
        return "config_entry_id" in message and (
            "extra keys not allowed" in message
            or "not a valid value" in message
            or "unknown field" in message
        )

    def _player_readiness(self, entity_id: str) -> dict[str, Any]:
        """Return whether a player entity can be targeted right now."""
        if not entity_id:
            return {"ready": False, "reason": "player is required"}
        state = self.hass.states.get(entity_id)
        native = self._ma_players_by_entity.get(entity_id) or self._ma_players_by_id.get(entity_id)
        if native:
            available = native.get("available") is not False and native.get("state") not in {"unknown", "unavailable"}
            return {"ready": available, "reason": "" if available else "Music Assistant player is unavailable", "entity_id": entity_id, "state": native.get("state")}
        if state is None:
            return {"ready": False, "reason": "player entity not found", "entity_id": entity_id}
        if str(state.state or "").lower() in {"unknown", "unavailable"}:
            return {
                "ready": False,
                "reason": f"player is {state.state}",
                "entity_id": entity_id,
                "state": state.state,
            }
        if not self.music_assistant_base_urls() or not self.music_assistant_tokens():
            return {
                "ready": False,
                "reason": "Music Assistant 2.10 API is not configured",
                "entity_id": entity_id,
                "state": state.state,
            }
        return {"ready": True, "entity_id": entity_id, "state": state.state}

    def media_players_snapshot(self, *, include_artwork: bool = True) -> list[dict[str, Any]]:
        """Return a lightweight media_player snapshot."""
        media_states = list(self.hass.states.async_all("media_player"))
        entity_registry = er.async_get(self.hass)
        states_by_entity = {state.entity_id: state for state in media_states}
        states_by_queue_id: dict[str, Any] = {}
        for candidate in media_states:
            candidate_attrs = candidate.attributes or {}
            for queue_id in (
                candidate_attrs.get("active_queue"),
                candidate_attrs.get("queue_id"),
                candidate_attrs.get("mass_player_id"),
                candidate_attrs.get("player_id"),
            ):
                clean_queue_id = str(queue_id or "").strip()
                if clean_queue_id:
                    states_by_queue_id.setdefault(clean_queue_id, candidate)

        def artwork_values(attributes: dict[str, Any]) -> list[Any]:
            metadata = attributes.get("metadata") if isinstance(attributes.get("metadata"), dict) else {}
            return [
                attributes.get("entity_picture"),
                attributes.get("media_image_url"),
                attributes.get("album_art"),
                attributes.get("thumbnail"),
                attributes.get("image"),
                metadata.get("image"),
                metadata.get("artwork"),
                metadata.get("thumbnail"),
            ]

        players = []
        for state in media_states:
            attrs = state.attributes or {}
            registry_entry = entity_registry.async_get(state.entity_id) if entity_registry is not None else None
            registry_platform = str(getattr(registry_entry, "platform", "") or "").strip()
            registry_device_id = str(getattr(registry_entry, "device_id", "") or "").strip()
            registry_unique_id = str(getattr(registry_entry, "unique_id", "") or "").strip()
            active_queue = _first_non_empty(attrs.get("active_queue"), attrs.get("queue_id"))
            active_source = _first_non_empty(
                attrs.get("active_source"),
                attrs.get("current_source"),
                attrs.get("source"),
                attrs.get("app_name"),
                attrs.get("app_id"),
            )
            queue_state = None
            clean_active_queue = str(active_queue or "").strip()
            if clean_active_queue:
                queue_state = states_by_entity.get(clean_active_queue) or states_by_queue_id.get(clean_active_queue)
            queue_attrs = queue_state.attributes if queue_state is not None else {}
            state_value = state.state
            if (
                str(state_value or "").lower() not in {"playing", "buffering"}
                and str(queue_state.state if queue_state else "").lower() in {"playing", "buffering"}
            ):
                state_value = queue_state.state
            artwork_candidates: list[str] = []
            homeii_artwork_url = ""
            if include_artwork:
                for candidate in [*artwork_values(attrs), *artwork_values(queue_attrs)]:
                    clean_candidate = str(candidate or "").strip()
                    if clean_candidate and clean_candidate not in artwork_candidates:
                        artwork_candidates.append(clean_candidate)
                homeii_artwork_url = self._proxied_artwork_url(
                    *artwork_candidates,
                    attrs,
                    queue_attrs,
                )
            friendly_name = attrs.get("friendly_name") or state.entity_id
            media_title = _first_non_empty(attrs.get("media_title"), queue_attrs.get("media_title"))
            media_artist = _first_non_empty(attrs.get("media_artist"), queue_attrs.get("media_artist"))
            media_album_name = _first_non_empty(attrs.get("media_album_name"), queue_attrs.get("media_album_name"))
            media_content_id = _first_non_empty(attrs.get("media_content_id"), queue_attrs.get("media_content_id"))
            media_content_type = _first_non_empty(attrs.get("media_content_type"), queue_attrs.get("media_content_type"))
            media_duration = _first_non_empty(attrs.get("media_duration"), queue_attrs.get("media_duration"))
            media_position = _first_non_empty(attrs.get("media_position"), queue_attrs.get("media_position"))
            entity_picture = homeii_artwork_url or (artwork_candidates[0] if artwork_candidates else attrs.get("entity_picture"))
            player_attributes = {
                **attrs,
                "friendly_name": friendly_name,
                "registry_platform": registry_platform,
                "registry_device_id": registry_device_id,
                "registry_unique_id": registry_unique_id,
                "active_queue": active_queue,
                "active_source": active_source,
                "queue_active": bool(
                    active_queue
                    and (
                        str(state_value or "").lower() in {"playing", "buffering", "paused"}
                        or _clean_string(active_source) == _clean_string(active_queue)
                    )
                ),
                "entity_picture": entity_picture,
                "media_image_url": homeii_artwork_url or _first_non_empty(attrs.get("media_image_url"), queue_attrs.get("media_image_url")),
                "media_artist": media_artist,
                "media_album_name": media_album_name,
                "media_album_artist": _first_non_empty(attrs.get("media_album_artist"), queue_attrs.get("media_album_artist")),
                "media_content_id": media_content_id,
                "media_content_type": media_content_type,
                "media_duration": media_duration,
                "media_position": media_position,
                "media_title": media_title,
                "homeii_artwork_url": homeii_artwork_url,
            }
            players.append(
                {
                    "entity_id": state.entity_id,
                    "state": state_value,
                    "attributes": player_attributes,
                    "friendly_name": friendly_name,
                    "app_id": attrs.get("app_id"),
                    "source": attrs.get("source"),
                    "active_source": active_source,
                    "registry_platform": registry_platform,
                    "registry_device_id": registry_device_id,
                    "registry_unique_id": registry_unique_id,
                    "mass_player_type": attrs.get("mass_player_type"),
                    "mass_player_id": attrs.get("mass_player_id") or attrs.get("player_id"),
                    "active_queue": active_queue,
                    "queue_active": player_attributes["queue_active"],
                    "group_members": _safe_list(attrs.get("group_members")),
                    "entity_picture": entity_picture,
                    "media_image_url": homeii_artwork_url or _first_non_empty(attrs.get("media_image_url"), queue_attrs.get("media_image_url")),
                    "homeii_artwork_url": homeii_artwork_url,
                    "artwork_candidates": artwork_candidates[:5],
                    "media_artist": media_artist,
                    "media_album_name": media_album_name,
                    "media_album_artist": _first_non_empty(attrs.get("media_album_artist"), queue_attrs.get("media_album_artist")),
                    "media_content_id": media_content_id,
                    "media_content_type": media_content_type,
                    "media_duration": media_duration,
                    "media_position": media_position,
                    "media_title": media_title,
                    "volume_level": attrs.get("volume_level"),
                    "is_volume_muted": attrs.get("is_volume_muted"),
                }
            )
        return players

    def music_assistant_players_snapshot(self) -> list[dict[str, Any]]:
        """Return media players that look like Music Assistant players."""
        return [player for player in self.media_players_snapshot() if self.is_music_assistant_player(player)]

    def _resolve_ma_player_id(self, player: str) -> str:
        """Resolve a HA entity or MA player identifier to the native MA player id."""
        clean = _clean_string(player)
        if not clean:
            return ""
        known_player = self._ma_players_by_entity.get(clean) or self._ma_players_by_id.get(clean)
        if known_player:
            return _clean_string(
                known_player.get("raw_player_id")
                or known_player.get("mass_player_id")
                or known_player.get("active_queue")
                or clean
            )
        registry_entry = er.async_get(self.hass).async_get(clean)
        if (
            registry_entry is not None
            and _clean_string(getattr(registry_entry, "platform", "")) == "music_assistant"
        ):
            registry_player_id = _clean_string(getattr(registry_entry, "unique_id", ""))
            if registry_player_id:
                return registry_player_id
        state = self.hass.states.get(clean)
        if state is None:
            return clean
        attrs = state.attributes or {}
        return _clean_string(
            _first_non_empty(
                attrs.get("mass_player_id"),
                attrs.get("player_id"),
                attrs.get("active_queue"),
                attrs.get("queue_id"),
                clean,
            )
        )

    def _ha_entity_for_ma_player(self, raw: dict[str, Any]) -> str:
        """Map a native MA player state back to its Home Assistant entity."""
        player_id = _clean_string(_dict_first(raw, "player_id", "id", "queue_id"))
        name = _clean_string(_dict_first(raw, "name", "display_name")).casefold()
        name_matches: list[str] = []
        # Queue ownership is shared by grouped players; it is never player identity.
        for entry in er.async_get(self.hass).entities.values():
            if entry.platform == "music_assistant" and entry.unique_id == player_id and entry.entity_id.startswith("media_player."):
                return entry.entity_id
        for player in self.media_players_snapshot(include_artwork=False):
            attrs = player.get("attributes") if isinstance(player.get("attributes"), dict) else {}
            identifiers = {
                _clean_string(value)
                for value in (
                    player.get("mass_player_id"),
                    player.get("registry_unique_id"),
                    attrs.get("mass_player_id"),
                    attrs.get("player_id"),
                    attrs.get("registry_unique_id"),
                )
                if _clean_string(value)
            }
            if player_id and player_id in identifiers:
                return _clean_string(player.get("entity_id"))
            friendly_name = _clean_string(player.get("friendly_name") or attrs.get("friendly_name")).casefold()
            if name and friendly_name == name and self.is_music_assistant_player(player):
                name_matches.append(_clean_string(player.get("entity_id")))
        unique_name_matches = [entity_id for entity_id in dict.fromkeys(name_matches) if entity_id]
        if len(unique_name_matches) == 1:
            return unique_name_matches[0]
        return f"media_player.homeii_{_safe_id_part(player_id)}" if player_id else ""

    def _normalize_ma_player(self, raw: Any) -> dict[str, Any] | None:
        """Normalize a MA 2.10 PlayerState for the card contract."""
        if not isinstance(raw, dict):
            return None
        player_id = _clean_string(_dict_first(raw, "player_id", "id", "queue_id"))
        if not player_id:
            return None
        current_media = raw.get("current_media") if isinstance(raw.get("current_media"), dict) else {}
        media = self.normalize_media_item(current_media, fallback_media_type="track") if current_media else None
        media = media or {}
        entity_id = self._ha_entity_for_ma_player(raw)
        volume = _maybe_number(_dict_first(raw, "volume_level", "group_volume"))
        volume_level = None if volume is None else max(0.0, min(1.0, volume / 100))
        playback_state = _clean_string(_dict_first(raw, "playback_state", "state")).lower() or "idle"
        if raw.get("available") is False:
            playback_state = "unavailable"
        active_source = _clean_string(raw.get("active_source"))
        active_queue = _clean_string(_dict_first(raw, "active_queue", "queue_id"))
        artwork_url = _clean_string(media.get("homeii_artwork_url"))
        friendly_name = _clean_string(_dict_first(raw, "name", "display_name") or entity_id or player_id)
        elapsed_updated = _maybe_number(
            _dict_first(raw, "elapsed_time_last_updated", "media_position_updated_at")
            or _dict_first(
                current_media,
                "elapsed_time_last_updated",
                "media_position_updated_at",
            )
        )
        if elapsed_updated is not None:
            elapsed_updated_at: str | None = datetime.fromtimestamp(
                elapsed_updated, tz=UTC
            ).isoformat()
        else:
            elapsed_updated_at = _clean_string(raw.get("media_position_updated_at")) or None
        attributes = {
            "friendly_name": friendly_name,
            "registry_platform": "music_assistant",
            "mass_player_type": _clean_string(_dict_first(raw, "type", "player_type", "provider")) or "player",
            "mass_player_id": player_id,
            "player_id": player_id,
            "active_queue": active_queue,
            "active_source": active_source,
            "queue_active": bool(active_queue),
            "available": raw.get("available", True),
            "powered": raw.get("powered", True),
            "volume_level": volume_level,
            "is_volume_muted": bool(_dict_first(raw, "volume_muted", "group_volume_muted")),
            "shuffle": bool(_dict_first(raw, "shuffle_enabled", "shuffle")),
            "repeat": _clean_string(_dict_first(raw, "repeat_mode", "repeat")) or "off",
            "group_members": _safe_list(raw.get("group_members")),
            "media_content_id": media.get("uri"),
            "media_content_type": media.get("media_type"),
            "media_title": media.get("name"),
            "media_artist": media.get("artist"),
            "media_album_name": media.get("album_name"),
            "media_duration": media.get("duration"),
            "media_position": _dict_first(raw, "elapsed_time", "media_position")
            or _dict_first(current_media, "elapsed_time", "media_position"),
            "media_position_updated_at": elapsed_updated_at,
            "entity_picture": artwork_url,
            "media_image_url": artwork_url,
            "homeii_artwork_url": artwork_url,
            "current_media": media,
            "supported_features": raw.get("supported_features") or [],
        }
        return {
            "entity_id": entity_id,
            "state": playback_state,
            "available": raw.get("available", True),
            "powered": raw.get("powered", True),
            "attributes": attributes,
            "friendly_name": friendly_name,
            "registry_platform": "music_assistant",
            "mass_player_type": attributes["mass_player_type"],
            "mass_player_id": player_id,
            "active_queue": active_queue,
            "queue_active": bool(active_queue),
            "active_source": active_source,
            "group_members": attributes["group_members"],
            "entity_picture": artwork_url,
            "media_image_url": artwork_url,
            "homeii_artwork_url": artwork_url,
            "media_title": media.get("name"),
            "media_artist": media.get("artist"),
            "media_album_name": media.get("album_name"),
            "media_content_id": media.get("uri"),
            "media_content_type": media.get("media_type"),
            "media_duration": media.get("duration"),
            "media_position": attributes["media_position"],
            "volume_level": volume_level,
            "is_volume_muted": attributes["is_volume_muted"],
            "raw_player_id": player_id,
        }

    async def async_players_snapshot(self) -> dict[str, Any]:
        """Return the authoritative MA 2.10 player catalog."""
        response = await self.async_music_assistant_command(
            {
                "command": "players/all",
                "args": {
                    "return_unavailable": True,
                    "return_disabled": False,
                    "return_protocol_players": False,
                },
            }
        )
        raw_players = response.get("data") if isinstance(response, dict) else response
        if not isinstance(raw_players, list):
            raise HomeiiFlowServiceUnavailable("Music Assistant returned an invalid player catalog.")
        queue_response = await self.async_music_assistant_command({"command": "player_queues/all", "args": {}})
        queues = queue_response.get("data") if isinstance(queue_response, dict) else queue_response
        if not isinstance(queues, list):
            raise HomeiiFlowServiceUnavailable("Music Assistant returned an invalid queue catalog.")
        queue_ids = {_clean_string(item.get("queue_id")) for item in queues if isinstance(item, dict)}
        resolved_players = []
        for raw in raw_players:
            if not isinstance(raw, dict):
                continue
            source = _clean_string(raw.get("active_source"))
            candidates = [raw.get("active_queue"), source, raw.get("synced_to"), raw.get("active_group")]
            if not source:
                candidates.append(raw.get("player_id"))
            queue_id = next((_clean_string(value) for value in candidates if value and _clean_string(value) in queue_ids), "")
            resolved_players.append({**raw, "active_queue": queue_id})
        players = [
            player
            for player in (self._normalize_ma_player(raw) for raw in resolved_players)
            if player
        ]
        self._ma_players_by_entity = {
            _clean_string(player.get("entity_id")): player
            for player in players
            if _clean_string(player.get("entity_id"))
        }
        self._ma_players_by_id = {
            _clean_string(player.get("raw_player_id") or player.get("mass_player_id")): player
            for player in players
            if _clean_string(player.get("raw_player_id") or player.get("mass_player_id"))
        }
        raw_by_id = {_clean_string(raw.get("player_id")): raw for raw in resolved_players}
        for player in players:
            native_id = _clean_string(player.get("raw_player_id") or player.get("mass_player_id"))
            raw = raw_by_id.get(native_id, {})
            leader_id = _clean_string(raw.get("synced_to")) or native_id
            leader = raw_by_id.get(leader_id, raw)
            native_members = _safe_list(leader.get("group_members"))
            mapped_members = []
            if native_members or raw.get("synced_to"):
                for member_id in dict.fromkeys([leader_id, *native_members, native_id]):
                    member = self._ma_players_by_id.get(_clean_string(member_id))
                    if member and member.get("entity_id"):
                        mapped_members.append(member["entity_id"])
            player["group_members"] = mapped_members
            player["attributes"]["group_members"] = mapped_members
        self._ma_http_health.update(
            {
                "connected": True,
                "authenticated": True,
                "player_count": len(players),
                "players_checked_at": _utc_iso(),
            }
        )
        return {
            "provider": "music_assistant.server_command:players/all",
            "source_of_truth": "homeii_flow_engine",
            "players": players,
            "music_assistant_players": players,
            "music_assistant_count": len(players),
            "snapshot": self._snapshot_meta("players", identity="music_assistant"),
        }

    @staticmethod
    def is_music_assistant_player(player: dict[str, Any]) -> bool:
        """Return whether a media player has Music Assistant markers."""
        attributes = player.get("attributes") if isinstance(player.get("attributes"), dict) else {}
        identity = " ".join(
            str(player.get(key) or "")
            for key in ("entity_id", "friendly_name", "app_id", "source", "registry_platform", "mass_player_type", "mass_player_id", "active_queue")
        ).lower()
        return bool(
            player.get("registry_platform") == "music_assistant"
            or attributes.get("registry_platform") == "music_assistant"
            or player.get("app_id") == "music_assistant"
            or player.get("mass_player_type")
            or player.get("mass_player_id")
            or player.get("active_queue")
            or "music_assistant" in identity
            or "music assistant" in identity
        )

    def stats(self, *, include_artwork: bool = True) -> dict[str, Any]:
        """Return a state snapshot for diagnostics and future dashboards."""
        all_players = self.media_players_snapshot(include_artwork=include_artwork)
        music_assistant_players = [player for player in all_players if self.is_music_assistant_player(player)]
        active_queue_counts: dict[str, int] = {}
        for player in music_assistant_players:
            queue_id = str(player.get("active_queue") or "").strip()
            if queue_id:
                active_queue_counts[queue_id] = active_queue_counts.get(queue_id, 0) + 1
        grouped = [
            player
            for player in music_assistant_players
            if len(_safe_list(player.get("group_members"))) > 1
            or (str(player.get("active_queue") or "").strip() and active_queue_counts.get(str(player.get("active_queue") or "").strip(), 0) > 1)
        ]
        playing = [player for player in music_assistant_players if str(player.get("state") or "").lower() in {"playing", "buffering"}]
        active_player = playing[0] if playing else {}
        return {
            "generated_at": _utc_iso(),
            "players_total": len(music_assistant_players),
            "all_media_players_total": len(all_players),
            "players_playing": len(playing),
            "players_grouped": len(grouped),
            "music_assistant_players": len(music_assistant_players),
            "active_player": active_player,
            "active_player_entity": active_player.get("entity_id") or "",
            "playing_entities": [player["entity_id"] for player in playing],
            "grouped_entities": [player["entity_id"] for player in grouped],
            "music_assistant_entities": [player["entity_id"] for player in music_assistant_players],
            "active_queue_groups": {
                queue_id: count
                for queue_id, count in active_queue_counts.items()
                if count > 1
            },
            "services": self.services_snapshot(),
        }

    def players_snapshot(
        self,
        *,
        include_all: bool = False,
        all_players: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return player state for card-side Engine features."""
        all_players = all_players if all_players is not None else self.media_players_snapshot()
        music_assistant_players = [player for player in all_players if self.is_music_assistant_player(player)]
        active_queue_counts: dict[str, int] = {}
        for player in music_assistant_players:
            queue_id = str(player.get("active_queue") or "").strip()
            if queue_id:
                active_queue_counts[queue_id] = active_queue_counts.get(queue_id, 0) + 1
        payload: dict[str, Any] = {
            "generated_at": _utc_iso(),
            "snapshot": self._snapshot_meta("players", identity="music_assistant"),
            "players": music_assistant_players,
            "music_assistant_players": music_assistant_players,
            "music_assistant_count": len(music_assistant_players),
            "all_media_players_total": len(all_players),
            "active_queue_groups": {
                queue_id: count
                for queue_id, count in active_queue_counts.items()
                if count > 1
            },
        }
        if include_all:
            payload["all_media_players"] = all_players
        return payload

    def cached_stats(self, max_age: float = 3.0) -> dict[str, Any]:
        """Return a short-lived stats snapshot for entities."""
        now = datetime.now(UTC).timestamp()
        if self._stats_cache is not None and now - self._stats_cache_at <= max_age:
            return self._stats_cache
        self._stats_cache = self.stats(include_artwork=False)
        self._stats_cache_at = now
        return self._stats_cache

    def _playback_stats_storage(self) -> dict[str, Any]:
        """Return normalized playback statistics storage."""
        stats = self._storage.get("playback_stats")
        if not isinstance(stats, dict):
            stats = {}
            self._storage["playback_stats"] = stats
        if not isinstance(stats.get("days"), dict):
            stats["days"] = {}
        if not isinstance(stats.get("players"), dict):
            stats["players"] = {}
        if not isinstance(stats.get("last_seen"), dict):
            stats["last_seen"] = {}
        return stats

    def _playback_stats_day(self, day_key: str) -> dict[str, Any]:
        """Return normalized playback statistics for one local day."""
        stats = self._playback_stats_storage()
        days = stats["days"]
        day = days.get(day_key)
        if not isinstance(day, dict):
            day = {"total_seconds": 0, "sessions": 0, "players": {}}
            days[day_key] = day
        if not isinstance(day.get("players"), dict):
            day["players"] = {}
        day["total_seconds"] = float(day.get("total_seconds") or 0)
        day["sessions"] = int(day.get("sessions") or 0)
        return day

    def _playback_stats_player(self, player: dict[str, Any]) -> dict[str, Any]:
        """Return normalized playback statistics for one player."""
        entity_id = str(player.get("entity_id") or "").strip()
        stats = self._playback_stats_storage()
        players = stats["players"]
        item = players.get(entity_id)
        if not isinstance(item, dict):
            item = {"total_seconds": 0, "sessions": 0}
            players[entity_id] = item
        item["friendly_name"] = player.get("friendly_name") or entity_id
        item["total_seconds"] = float(item.get("total_seconds") or 0)
        item["sessions"] = int(item.get("sessions") or 0)
        item["last_state"] = player.get("state")
        item["last_title"] = player.get("media_title")
        item["last_seen_at"] = _utc_iso()
        return item

    def _sync_playback_statistics(self, now: datetime | None = None) -> dict[str, Any]:
        """Synchronize passive playback statistics from current player states."""
        local_now = _local_datetime(now)
        playing = [
            player
            for player in self.music_assistant_players_snapshot()
            if str(player.get("state") or "").lower() in {"playing", "buffering"}
        ]
        if self._playback_stats_last_tick_at is None:
            elapsed_seconds = 0.0
        else:
            elapsed_seconds = max(0.0, min(300.0, (local_now - self._playback_stats_last_tick_at).total_seconds()))
        self._playback_stats_last_tick_at = local_now
        day_key = local_now.date().isoformat()
        day = self._playback_stats_day(day_key)
        active_entities = {str(player.get("entity_id") or "").strip() for player in playing if player.get("entity_id")}
        started_entities = active_entities - self._playback_stats_active_entities
        ended_entities = self._playback_stats_active_entities - active_entities

        if elapsed_seconds > 0 and playing:
            day["total_seconds"] = float(day.get("total_seconds") or 0) + elapsed_seconds * len(playing)
            for player in playing:
                entity_id = str(player.get("entity_id") or "").strip()
                if not entity_id:
                    continue
                player_stats = self._playback_stats_player(player)
                player_stats["total_seconds"] = float(player_stats.get("total_seconds") or 0) + elapsed_seconds
                day_players = day["players"]
                day_player = day_players.get(entity_id)
                if not isinstance(day_player, dict):
                    day_player = {"total_seconds": 0, "sessions": 0}
                    day_players[entity_id] = day_player
                day_player["friendly_name"] = player.get("friendly_name") or entity_id
                day_player["total_seconds"] = float(day_player.get("total_seconds") or 0) + elapsed_seconds
                day_player["last_title"] = player.get("media_title")
                self._playback_stats_dirty = True

        if started_entities:
            day["sessions"] = int(day.get("sessions") or 0) + len(started_entities)
            for player in playing:
                entity_id = str(player.get("entity_id") or "").strip()
                if entity_id not in started_entities:
                    continue
                player_stats = self._playback_stats_player(player)
                player_stats["sessions"] = int(player_stats.get("sessions") or 0) + 1
                day_player = day["players"].setdefault(entity_id, {"total_seconds": 0, "sessions": 0})
                day_player["friendly_name"] = player.get("friendly_name") or entity_id
                day_player["sessions"] = int(day_player.get("sessions") or 0) + 1
                day_player["last_title"] = player.get("media_title")
            self._playback_stats_dirty = True

        if ended_entities:
            self._playback_stats_dirty = True

        self._playback_stats_active_entities = active_entities
        stats = self._playback_stats_storage()
        stats["last_updated_at"] = _utc_iso()
        stats["active_entities"] = sorted(active_entities)
        return {
            "local_now": local_now,
            "playing": playing,
            "active_entities": active_entities,
            "started_entities": started_entities,
            "ended_entities": ended_entities,
            "elapsed_seconds": elapsed_seconds,
        }

    async def async_update_playback_statistics(self, now: datetime | None = None) -> dict[str, Any]:
        """Update passive playback statistics from current Music Assistant player states."""
        result = self._sync_playback_statistics(now)
        local_now = result["local_now"]
        started_entities = result["started_entities"]
        ended_entities = result["ended_entities"]

        if started_entities:
            await self.async_record_activity(
                "playback_started",
                f"Playback started on {len(started_entities)} player(s)",
                data={"players": sorted(started_entities)},
            )

        if ended_entities:
            await self.async_record_activity(
                "playback_stopped",
                f"Playback stopped on {len(ended_entities)} player(s)",
                data={"players": sorted(ended_entities)},
            )

        now_ts = datetime.now(UTC).timestamp()
        if self._playback_stats_dirty and now_ts - self._playback_stats_last_save_at >= 240:
            self._playback_stats_last_save_at = now_ts
            self._playback_stats_dirty = False
            await self.async_save()
        return self.playback_statistics(local_now)

    def playback_statistics(self, now: datetime | None = None) -> dict[str, Any]:
        """Return passive playback statistics for dashboards and diagnostics."""
        local_now = _local_datetime(now)
        if not self._playback_stats_is_syncing:
            self._playback_stats_is_syncing = True
            try:
                self._sync_playback_statistics(local_now)
            finally:
                self._playback_stats_is_syncing = False
        day_key = local_now.date().isoformat()
        stats = self._playback_stats_storage()
        day = self._playback_stats_day(day_key)
        day_players = [
            {
                "entity_id": entity_id,
                "friendly_name": item.get("friendly_name") or entity_id,
                "seconds": round(float(item.get("total_seconds") or 0), 1),
                "minutes": round(float(item.get("total_seconds") or 0) / 60, 1),
                "sessions": int(item.get("sessions") or 0),
                "last_title": item.get("last_title"),
            }
            for entity_id, item in day.get("players", {}).items()
            if isinstance(item, dict)
        ]
        day_players.sort(key=lambda item: item.get("seconds", 0), reverse=True)
        active_entities = _safe_list(stats.get("active_entities"))
        recommendation = self.screensaver_recommendation(now)
        return {
            "generated_at": _utc_iso(),
            "day": day_key,
            "today_seconds": round(float(day.get("total_seconds") or 0), 1),
            "today_minutes": round(float(day.get("total_seconds") or 0) / 60, 1),
            "today_sessions": int(day.get("sessions") or 0),
            "top_player_today": day_players[0] if day_players else {},
            "players_today": day_players[:12],
            "active_entities": active_entities,
            "active_count": len(active_entities),
            "screensaver": recommendation,
            "last_updated_at": stats.get("last_updated_at") or "",
        }

    def screensaver_recommendation(self, now: datetime | None = None) -> dict[str, Any]:
        """Return a safe Engine-side screensaver recommendation."""
        stats = self.cached_stats()
        playing_entities = _safe_list(stats.get("playing_entities"))
        active_player = stats.get("active_player") if isinstance(stats.get("active_player"), dict) else {}
        mode = "lyrics" if playing_entities else "clock"
        return {
            "mode": mode,
            "reason": "music_playing" if playing_entities else "idle",
            "active_player": active_player,
            "active_player_entity": active_player.get("entity_id") if active_player else "",
            "playing_entities": playing_entities,
            "generated_at": _utc_iso(),
        }

    def screensaver_config(self, profile_id: str | None = None) -> dict[str, Any]:
        """Return system-wide screensaver configuration for a profile."""
        clean_profile = str(profile_id or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        config = self._screensaver_raw_config(clean_profile)
        ma_url_configured = any(
            _normalized_http_url(config.get(key))
            for key in ("music_assistant_url", "ma_url", "music_assistant_external_url")
        )
        return {
            "profile_id": clean_profile,
            "enabled": bool(config.get("enabled", False)),
            "timeout_seconds": _bounded_int(config.get("timeout_seconds"), 90, 15, 3600),
            "mode": str(config.get("mode") or "auto").strip().lower() if str(config.get("mode") or "auto").strip().lower() in {"auto", "clock", "lyrics"} else "auto",
            "auto_lyrics_when_playing": bool(config.get("auto_lyrics_when_playing", True)),
            "clock_mode": str(config.get("clock_mode") or "digital").strip().lower() if str(config.get("clock_mode") or "digital").strip().lower() in {"digital", "analog"} else "digital",
            "message": str(config.get("message") or "").strip()[:120],
            "show_artwork": bool(config.get("show_artwork", True)),
            "show_request_id": str(config.get("show_request_id") or ""),
            "show_requested_at": str(config.get("show_requested_at") or ""),
            "show_request_expires_at": str(config.get("show_request_expires_at") or ""),
            "show_source": str(config.get("show_source") or ""),
            "updated_at": str(config.get("updated_at") or ""),
            "music_assistant_url_configured": ma_url_configured,
            "frontend_url": "/homeii_flow/homeii-flow-system-screensaver.js",
        }

    async def async_set_screensaver_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store system-wide screensaver configuration."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        current_raw = self._screensaver_raw_config(profile_id)
        current = self.screensaver_config(profile_id)
        next_config = {
            **current_raw,
            "enabled": bool(payload.get("enabled", current.get("enabled", False))),
            "timeout_seconds": _bounded_int(payload.get("timeout_seconds", current.get("timeout_seconds")), 90, 15, 3600),
            "mode": str(payload.get("mode", current.get("mode", "auto")) or "auto").strip().lower(),
            "auto_lyrics_when_playing": bool(payload.get("auto_lyrics_when_playing", current.get("auto_lyrics_when_playing", True))),
            "clock_mode": str(payload.get("clock_mode", current.get("clock_mode", "digital")) or "digital").strip().lower(),
            "message": str(payload.get("message", current.get("message", "")) or "").strip()[:120],
            "show_artwork": bool(payload.get("show_artwork", current.get("show_artwork", True))),
            "updated_at": _utc_iso(),
        }
        raw_ma_url = _first_non_empty(
            payload.get("music_assistant_url"),
            payload.get("ma_url"),
            payload.get("music_assistant_external_url"),
        )
        if raw_ma_url is not None:
            next_config["music_assistant_url"] = _normalized_http_url(raw_ma_url)
            next_config.pop("ma_url", None)
            next_config.pop("music_assistant_external_url", None)
        if next_config["mode"] not in {"auto", "clock", "lyrics"}:
            next_config["mode"] = "auto"
        if next_config["clock_mode"] not in {"digital", "analog"}:
            next_config["clock_mode"] = "digital"
        next_config.pop("frontend_url", None)
        next_config.pop("music_assistant_url_configured", None)
        store = self._screensaver_store()
        store[profile_id] = next_config
        await self.async_save()
        await self.async_record_activity(
            "screensaver_config_saved",
            f"System screensaver {'enabled' if next_config['enabled'] else 'disabled'}",
            profile_id=profile_id,
            data={"timeout_seconds": next_config["timeout_seconds"], "mode": next_config["mode"]},
        )
        return self.screensaver_state(profile_id)

    async def async_request_screensaver_show(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask every loaded frontend screensaver agent to open once."""
        payload = payload or {}
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        now = datetime.now(UTC)
        current = self._screensaver_raw_config(profile_id)
        next_config = {
            **current,
            "show_request_id": uuid4().hex,
            "show_requested_at": now.isoformat(),
            "show_request_expires_at": (now + timedelta(minutes=2)).isoformat(),
            "show_source": str(payload.get("source") or "integration").strip()[:60],
            "updated_at": now.isoformat(),
        }
        store = self._screensaver_store()
        store[profile_id] = next_config
        await self.async_save()
        await self.async_record_activity(
            "screensaver_show_requested",
            "System screensaver show requested",
            profile_id=profile_id,
            data={
                "show_request_id": next_config["show_request_id"],
                "expires_at": next_config["show_request_expires_at"],
                "source": next_config["show_source"],
            },
        )
        return self.screensaver_state(profile_id)

    def screensaver_state(self, profile_id: str | None = None) -> dict[str, Any]:
        """Return system-wide screensaver state for frontend agents."""
        config = self.screensaver_config(profile_id)
        recommendation = self.screensaver_recommendation()
        effective_mode = config.get("mode") or "auto"
        if effective_mode == "auto":
            effective_mode = "lyrics" if config.get("auto_lyrics_when_playing") and recommendation.get("mode") == "lyrics" else "clock"
        return {
            "config": config,
            "recommendation": recommendation,
            "effective_mode": effective_mode,
            "enabled": bool(config.get("enabled")),
            "timeout_seconds": config.get("timeout_seconds"),
            "generated_at": _utc_iso(),
        }

    async def async_play_media(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Play media through the authoritative MA 2.10 queue API."""
        player = str(payload.get("player") or payload.get("entity_id") or payload.get("selected_player") or "").strip()
        media_id = str(
            payload.get("media_id")
            or payload.get("media_content_id")
            or payload.get("uri")
            or ""
        ).strip()
        media_type = str(payload.get("media_type") or payload.get("media_content_type") or "music").strip() or "music"
        enqueue = str(payload.get("enqueue") or "play").strip() or "play"
        radio_mode = bool(payload.get("radio_mode"))
        if not player or not media_id:
            raise ValueError("player and media_id are required")
        verify_playback = bool(payload.get("verify_playback")) and enqueue in {"play", "replace", "shuffle"}
        readiness = self._player_readiness(player)
        if verify_playback and not readiness.get("ready"):
            raise HomeiiFlowServiceUnavailable(str(readiness.get("reason") or "player is not ready"))
        before_snapshot: dict[str, Any] = {}

        ma_player_id = self._resolve_ma_player_id(player)
        queue_response = await self.async_music_assistant_command(
            {
                "command": "player_queues/get_active_queue",
                "args": {"player_id": ma_player_id},
            }
        )
        active_queue = queue_response.get("data") if isinstance(queue_response, dict) else queue_response
        if isinstance(active_queue, dict):
            before_snapshot = copy.deepcopy(active_queue)
        queue_id = _clean_string(
            _dict_first(active_queue, "queue_id", "active_queue", "player_id")
            if isinstance(active_queue, dict)
            else ""
        ) or ma_player_id
        shuffle = enqueue == "shuffle"
        queue_option = {
            "play": "replace",
            "replace": "replace",
            "add": "add",
            "next": "next",
            "replace_next": "replace_next",
        }.get(enqueue, "replace")
        await self.async_music_assistant_command(
            {
                "command": "player_queues/play_media",
                "args": {
                    "queue_id": queue_id,
                    "media": media_id,
                    "option": queue_option,
                    "radio_mode": radio_mode,
                },
            }
        )
        if shuffle:
            await self.async_music_assistant_command(
                {
                    "command": "player_queues/shuffle",
                    "args": {"queue_id": queue_id, "shuffle_enabled": True},
                }
            )
        # Verification follows the authoritative MA queue, including native-only players.
        # Merely still playing the previous song is not confirmation of this request.
        verified = False
        after_snapshot: dict[str, Any] = {}
        self._stats_cache = None
        self._bump_snapshot_revision("players", "queue", reason="play_media")
        if verify_playback:
            before_item = before_snapshot.get("current_item") or {}
            for _ in range(20):
                response = await self.async_music_assistant_command({"command": "player_queues/get", "args": {"queue_id": queue_id}})
                after_snapshot = response.get("data") if isinstance(response, dict) else response
                after_snapshot = after_snapshot if isinstance(after_snapshot, dict) else {}
                current_item = after_snapshot.get("current_item") or {}
                changed_item = bool(current_item.get("queue_item_id") and current_item.get("queue_item_id") != before_item.get("queue_item_id"))
                if after_snapshot.get("state") == "playing" and changed_item:
                    verified = True
                    break
                await asyncio.sleep(0.4)
            if not verified:
                raise HomeiiFlowServiceUnavailable(
                    "Music Assistant accepted the queue command, but the player did not confirm playback."
                )
        return {
            "ok": True,
            "player": player,
            "player_id": ma_player_id,
            "queue_id": queue_id,
            "media_id": media_id,
            "media_type": media_type,
            "enqueue": enqueue,
            "provider": "music_assistant.server_command:player_queues/play_media",
            "verified": verified,
            "readiness": readiness,
            "player_before": before_snapshot,
            "player_after": after_snapshot,
            "executed_at": _utc_iso(),
        }

    async def async_player_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a player command through the MA 2.10 player API."""
        player = str(payload.get("player") or payload.get("entity_id") or payload.get("selected_player") or "").strip()
        command = str(payload.get("command") or payload.get("action") or "").strip().lower()
        if not player or not command:
            raise ValueError("player and command are required")

        player_id = self._resolve_ma_player_id(player)
        api_command = ""
        args: dict[str, Any] = {"player_id": player_id}
        active_queue_snapshot: dict[str, Any] = {}

        async def active_queue_id() -> str:
            nonlocal active_queue_snapshot
            queue_response = await self.async_music_assistant_command(
                {"command": "player_queues/get_active_queue", "args": {"player_id": player_id}}
            )
            active_queue = queue_response.get("data", queue_response) if isinstance(queue_response, dict) else queue_response
            active_queue_snapshot = active_queue if isinstance(active_queue, dict) else {}
            return _clean_string(
                _dict_first(active_queue, "queue_id", "active_queue", "player_id")
                if isinstance(active_queue, dict)
                else ""
            ) or player_id

        if command in {"play", "media_play"}:
            api_command = "players/cmd/play"
        elif command in {"pause", "media_pause"}:
            api_command = "players/cmd/pause"
        elif command in {"stop", "media_stop"}:
            api_command = "players/cmd/stop"
        elif command in {"next", "next_track", "media_next_track"}:
            api_command = "players/cmd/next"
        elif command in {"previous", "prev", "previous_track", "media_previous_track"}:
            api_command = "players/cmd/previous"
        elif command in {"shuffle", "shuffle_set"}:
            api_command = "player_queues/shuffle"
            args = {
                "queue_id": await active_queue_id(),
                "shuffle_enabled": bool(payload.get("shuffle", True)),
            }
        elif command in {"repeat", "repeat_set"}:
            api_command = "player_queues/repeat"
            repeat = _clean_string(payload.get("repeat") or payload.get("repeat_mode") or "off").lower()
            args = {
                "queue_id": await active_queue_id(),
                "repeat_mode": repeat if repeat in {"off", "one", "all"} else "off",
            }
        elif command in {"volume", "volume_set"}:
            api_command = "players/cmd/volume_set"
            volume = payload.get("volume_level") if payload.get("volume_level") is not None else payload.get("volume")
            numeric = float(volume)
            args["volume_level"] = round(max(0.0, min(1.0, numeric if numeric <= 1 else numeric / 100)) * 100)
        elif command in {"mute", "unmute", "volume_mute"}:
            api_command = "players/cmd/volume_mute"
            args["muted"] = command == "mute" or bool(payload.get("is_volume_muted"))
        elif command in {"unjoin", "ungroup"}:
            api_command = "players/cmd/ungroup"
        elif command in {"join", "group"}:
            api_command = "players/cmd/group_many"
            args = {
                "target_player": player_id,
                "child_player_ids": [self._resolve_ma_player_id(str(member)) for member in _safe_list(payload.get("group_members")) if str(member) != player],
            }
        elif command in {"seek", "media_seek"}:
            api_command = "player_queues/seek"
            position = payload.get("seek_position") if payload.get("seek_position") is not None else payload.get("position")
            args = {"queue_id": await active_queue_id(), "position": max(0, round(float(position)))}
        elif command == "playback_speed":
            queue_id = await active_queue_id()
            current_item = active_queue_snapshot.get("current_item") or {}
            media_item = current_item.get("media_item") or {}
            if media_item.get("media_type") not in {"podcast_episode", "audiobook"}:
                raise ValueError("Playback speed is available only for podcast episodes and audiobooks. Select one before changing speed.")
            api_command, args = build_playback_speed(queue_id, payload)
        elif command in {"clear", "clear_playlist"}:
            api_command = "player_queues/clear"
            args = {"queue_id": await active_queue_id()}
        elif command in {"autoplay", "autoplay_set", "dont_stop_the_music", "crossfade", "crossfade_set"}:
            api_command, args = build_queue_switch(command, await active_queue_id(), payload)
        else:
            raise ValueError(f"Unsupported player command: {command}")
        await self.async_music_assistant_command({"command": api_command, "args": args})
        self._stats_cache = None
        self._bump_snapshot_revision("players", "queue", reason=f"player_command:{command}")
        return {
            "ok": True,
            "player": player,
            "player_id": player_id,
            "command": command,
            "provider": f"music_assistant.server_command:{api_command}",
            "executed_at": _utc_iso(),
        }

    async def async_transfer_queue(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Transfer the current Music Assistant queue between players."""
        source_player = str(payload.get("source_player") or payload.get("source_entity_id") or "").strip()
        target_player = str(
            payload.get("target_player")
            or payload.get("target_entity_id")
            or payload.get("entity_id")
            or payload.get("selected_player")
            or ""
        ).strip()
        auto_play = bool(payload.get("auto_play", True))
        if not source_player or not target_player:
            raise ValueError("source_player and target_player are required")
        source_id = self._resolve_ma_player_id(source_player)
        target_id = self._resolve_ma_player_id(target_player)
        source_queue_response = await self.async_music_assistant_command(
            {"command": "player_queues/get_active_queue", "args": {"player_id": source_id}}
        )
        target_queue_response = await self.async_music_assistant_command(
            {"command": "player_queues/get_active_queue", "args": {"player_id": target_id}}
        )
        source_queue = source_queue_response.get("data") if isinstance(source_queue_response, dict) else source_queue_response
        target_queue = target_queue_response.get("data") if isinstance(target_queue_response, dict) else target_queue_response
        source_queue_id = _clean_string(_dict_first(source_queue, "queue_id", "player_id") if isinstance(source_queue, dict) else "") or source_id
        target_queue_id = _clean_string(_dict_first(target_queue, "queue_id", "player_id") if isinstance(target_queue, dict) else "") or target_id
        if source_queue_id == target_queue_id:
            return {"ok": True, "noop": True, "source_queue_id": source_queue_id, "target_queue_id": target_queue_id}
        await self.async_music_assistant_command(
            {
                "command": "player_queues/transfer",
                "args": {
                    "source_queue_id": source_queue_id,
                    "target_queue_id": target_queue_id,
                    "auto_play": auto_play,
                },
            }
        )
        self._stats_cache = None
        self._bump_snapshot_revision("players", "queue", reason="queue_transfer")
        return {
            "ok": True,
            "source_player": source_player,
            "target_player": target_player,
            "source_queue_id": source_queue_id,
            "target_queue_id": target_queue_id,
            "auto_play": auto_play,
            "provider": "music_assistant.server_command:player_queues/transfer",
            "executed_at": _utc_iso(),
        }

    async def async_queue_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a queue item action through the Engine."""
        player = str(payload.get("player") or payload.get("entity_id") or payload.get("selected_player") or "").strip()
        queue_id = str(payload.get("queue_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        queue_item_id = str(payload.get("queue_item_id") or payload.get("item_id") or "").strip()
        if not player:
            raise ValueError("player is required")
        if not action:
            raise ValueError("action is required")
        if not queue_item_id:
            raise ValueError("queue_item_id is required")
        if action not in {"up", "down", "next", "remove", "move_to"}:
            raise ValueError(f"Unsupported queue action: {action}")
        player_id = self._resolve_ma_player_id(player)
        clean_queue_id = queue_id
        if not clean_queue_id:
            queue_response = await self.async_music_assistant_command(
                {"command": "player_queues/get_active_queue", "args": {"player_id": player_id}}
            )
            active_queue = queue_response.get("data") if isinstance(queue_response, dict) else queue_response
            clean_queue_id = _clean_string(
                _dict_first(active_queue, "queue_id", "active_queue", "player_id")
                if isinstance(active_queue, dict)
                else ""
            ) or player_id
        direct_args: dict[str, Any] | None = None
        direct_command = ""
        if clean_queue_id and action == "remove":
            direct_command = "player_queues/delete_item"
            direct_args = {
                "queue_id": clean_queue_id,
                "item_id_or_index": queue_item_id,
            }
        elif clean_queue_id and action in {"up", "down", "next", "move_to"}:
            if action == "next":
                # MA defines zero as the first upcoming position, not one step down.
                direct_shift = 0
            elif action == "move_to":
                try:
                    direct_shift = int(payload["position_shift"])
                except (KeyError, TypeError, ValueError) as err:
                    raise ValueError("A valid position_shift is required for move_to") from err
            else:
                direct_shift = {"up": -1, "down": 1}[action]
            if direct_shift == 0 and action == "move_to":
                return {
                    "ok": True,
                    "player": player,
                    "queue_id": clean_queue_id,
                    "queue_item_id": queue_item_id,
                    "action": action,
                    "provider": "homeii_flow_engine.queue_noop",
                    "position_shift": 0,
                    "executed_at": _utc_iso(),
                }
            direct_command = "player_queues/move_item"
            direct_args = {
                "queue_id": clean_queue_id,
                "queue_item_id": queue_item_id,
                "pos_shift": direct_shift,
            }
        if direct_command and direct_args:
            try:
                await self.async_music_assistant_command(
                    {"command": direct_command, "args": direct_args}
                )
                self._stats_cache = None
                self._bump_snapshot_revision("queue", "players", reason=f"queue_action:{action}")
                return {
                    "ok": True,
                    "player": player,
                    "queue_id": clean_queue_id,
                    "queue_item_id": queue_item_id,
                    "action": action,
                    "provider": f"music_assistant.server_command:{direct_command}",
                    "position_shift": direct_args.get("pos_shift"),
                    "executed_at": _utc_iso(),
                }
            except Exception as err:
                raise HomeiiFlowServiceUnavailable(str(err)) from err
        raise HomeiiFlowServiceUnavailable(
            "Music Assistant could not resolve the active queue for this action."
        )

    def schedule_count(self, profile_id: str | None = None) -> int:
        """Return schedule count."""
        return len(self.schedules(profile_id))

    def schedule_summaries(self, profile_id: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        """Return user-facing schedule details for sensors and diagnostics."""
        local_now = _local_datetime(now)
        summaries: list[dict[str, Any]] = []
        for schedule in self.schedules(profile_id):
            next_run = _next_schedule_datetime(schedule, local_now)
            summaries.append(
                {
                    "id": schedule.get("id"),
                    "name": schedule.get("name"),
                    "enabled": bool(schedule.get("enabled", True)),
                    "time": schedule.get("time"),
                    "days": _schedule_days(schedule),
                    "player": schedule.get("player"),
                    "media_id": schedule.get("media_id"),
                    "media_type": schedule.get("media_type"),
                    "media_name": schedule.get("media_name"),
                    "media_mode": schedule.get("media_mode"),
                    "playlist": schedule.get("playlist"),
                    "playlist_name": schedule.get("playlist_name"),
                    "enqueue": schedule.get("enqueue"),
                    "retry_attempts": schedule.get("retry_attempts"),
                    "retry_delay": schedule.get("retry_delay"),
                    "volume": schedule.get("volume"),
                    "after_run": schedule.get("after_run"),
                    "updated_at": schedule.get("updated_at"),
                    "due_now": self._schedule_due(schedule, local_now),
                    "next_run": next_run.isoformat() if next_run else "",
                }
            )
        return summaries

    def next_schedule_summary(self, profile_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Return the next enabled schedule for a profile."""
        local_now = _local_datetime(now)
        best_schedule: dict[str, Any] | None = None
        best_run: datetime | None = None
        for schedule in self.schedules(profile_id):
            next_run = _next_schedule_datetime(schedule, local_now)
            if next_run is None:
                continue
            if best_run is None or next_run < best_run:
                best_schedule = schedule
                best_run = next_run
        if best_schedule is None or best_run is None:
            return {}
        return {
            "id": best_schedule.get("id"),
            "name": best_schedule.get("name"),
            "enabled": bool(best_schedule.get("enabled", True)),
            "time": best_schedule.get("time"),
            "days": _schedule_days(best_schedule),
            "player": best_schedule.get("player"),
            "media_id": best_schedule.get("media_id"),
            "media_type": best_schedule.get("media_type"),
            "media_name": best_schedule.get("media_name"),
            "media_mode": best_schedule.get("media_mode"),
            "playlist": best_schedule.get("playlist"),
            "playlist_name": best_schedule.get("playlist_name"),
            "enqueue": best_schedule.get("enqueue"),
            "retry_attempts": best_schedule.get("retry_attempts"),
            "retry_delay": best_schedule.get("retry_delay"),
            "volume": best_schedule.get("volume"),
            "after_run": best_schedule.get("after_run"),
            "next_run": best_run.isoformat(),
        }

    def timer_summaries(self, profile_id: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        """Return user-facing timer details for sensors and diagnostics."""
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        current_utc = current.astimezone(UTC)
        summaries: list[dict[str, Any]] = []
        for timer in self.timers(profile_id):
            ends_at = _parse_utc_datetime(timer.get("ends_at") or timer.get("target_at"))
            remaining = int((ends_at - current_utc).total_seconds()) if ends_at else 0
            summaries.append(
                {
                    "id": timer.get("id") or timer.get("timer_id"),
                    "enabled": bool(timer.get("enabled", True)),
                    "type": timer.get("type") or timer.get("timer_type"),
                    "player": timer.get("player"),
                    "action": timer.get("action"),
                    "minutes": timer.get("minutes"),
                    "ends_at": ends_at.isoformat() if ends_at else "",
                    "remaining_seconds": max(0, remaining),
                    "origin": timer.get("origin"),
                    "created_at": timer.get("created_at"),
                    "updated_at": timer.get("updated_at"),
                    "due_now": bool(ends_at and ends_at <= current_utc),
                }
            )
        return summaries

    def next_timer_summary(self, profile_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Return the next enabled one-shot timer for a profile."""
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        current_utc = current.astimezone(UTC)
        best_timer: dict[str, Any] | None = None
        best_run: datetime | None = None
        for timer in self.timers(profile_id):
            if not bool(timer.get("enabled", True)):
                continue
            ends_at = _parse_utc_datetime(timer.get("ends_at") or timer.get("target_at"))
            if ends_at is None:
                continue
            if best_run is None or ends_at < best_run:
                best_timer = timer
                best_run = ends_at
        if best_timer is None or best_run is None:
            return {}
        remaining = int((best_run - current_utc).total_seconds())
        return {
            "id": best_timer.get("id") or best_timer.get("timer_id"),
            "enabled": bool(best_timer.get("enabled", True)),
            "type": best_timer.get("type") or best_timer.get("timer_type"),
            "player": best_timer.get("player"),
            "action": best_timer.get("action"),
            "minutes": best_timer.get("minutes"),
            "ends_at": best_run.isoformat(),
            "remaining_seconds": max(0, remaining),
            "origin": best_timer.get("origin"),
        }

    def volume_rule_summaries(
        self,
        profile_id: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return user-facing volume rule details for sensors and diagnostics."""
        local_now = _local_datetime(now)
        return [
            {
                "profile_id": rule.get("profile_id") or DEFAULT_PROFILE_ID,
                "player": rule.get("player"),
                "enabled": bool(rule.get("enabled", True)),
                "max_volume": rule.get("max_volume"),
                "start_time": rule.get("start_time"),
                "end_time": rule.get("end_time"),
                "days": [int(day) for day in _safe_list(rule.get("days")) if str(day).strip()],
                "active_now": self._volume_rule_active(rule, local_now),
                "updated_at": rule.get("updated_at"),
            }
            for rule in self.volume_rules(profile_id)
        ]

    def active_volume_rule_summaries(
        self,
        profile_id: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return volume rules that are enabled and active now."""
        return [rule for rule in self.volume_rule_summaries(profile_id, now) if rule.get("active_now")]

    def timer_count(self, profile_id: str | None = None) -> int:
        """Return active timer count."""
        return len(self.timers(profile_id))

    def volume_rule_count(self, profile_id: str | None = None) -> int:
        """Return volume rule count."""
        return len(self.volume_rules(profile_id))

    def announcement_count(self, profile_id: str | None = None) -> int:
        """Return stored announcement count."""
        return len(self.announcements(profile_id))

    async def async_set_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store or update a schedule."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
        schedule_id = (
            str(payload.get("id") or payload.get("schedule_id") or "").strip()
            or f"schedule_{uuid4().hex[:12]}"
        )
        media_id = str(
            payload.get("media_id")
            or payload.get("media_content_id")
            or payload.get("playlist")
            or payload.get("uri")
            or ""
        ).strip()
        media_type = str(payload.get("media_type") or payload.get("media_content_type") or "playlist").strip() or "playlist"
        media_mode = _schedule_media_mode(payload.get("media_mode") or payload.get("selection_mode"), media_id)
        if media_mode == "random_playlist" and media_type == "music":
            media_type = "playlist"
        enqueue = str(payload.get("enqueue") or "play").strip().lower() or "play"
        if enqueue not in {"play", "add", "next", "replace", "shuffle"}:
            enqueue = "play"
        playlist_name = str(
            payload.get("playlist_name")
            or payload.get("playlistName")
            or payload.get("media_name")
            or ""
        ).strip()
        schedule = {
            "id": schedule_id,
            "profile_id": profile_id,
            "name": str(payload.get("name") or "HOMEii schedule").strip(),
            "player": str(payload.get("player") or payload.get("entity_id") or "").strip(),
            "kind": str(payload.get("kind") or payload.get("action") or "wake_playback").strip() or "wake_playback",
            "media_id": media_id,
            "media_type": media_type,
            "media_name": str(payload.get("media_name") or playlist_name or "").strip(),
            "media_mode": media_mode,
            "playlist": str(payload.get("playlist") or media_id or "").strip(),
            "playlist_name": playlist_name,
            "enqueue": enqueue,
            "radio_mode": bool(payload.get("radio_mode")),
            "retry_attempts": _bounded_int(payload.get("retry_attempts"), 4, 1, 12),
            "retry_delay": _bounded_int(payload.get("retry_delay"), 5, 1, 30),
            "time": str(payload.get("time") or "").strip(),
            "days": [int(day) for day in _safe_list(payload.get("days")) if str(day).strip()],
            "volume": payload.get("volume"),
            "enabled": bool(payload.get("enabled", True)),
            "after_run": "disable" if str(payload.get("after_run") or payload.get("afterRun") or "").strip() == "disable" else "keep",
            "updated_at": _utc_iso(),
        }
        schedules = [
            existing
            for existing in self.schedules()
            if not (existing.get("profile_id") == profile_id and existing.get("id") == schedule_id)
        ]
        schedules.append(schedule)
        self._storage["schedules"] = schedules
        await self.async_save()
        self._reschedule_all_schedules()
        self._stats_cache = None
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        await self.async_record_activity(
            "schedule_saved",
            f"Schedule saved: {schedule.get('name') or schedule_id}",
            profile_id=profile_id,
            data={"schedule_id": schedule_id, "player": schedule.get("player"), "time": schedule.get("time")},
        )
        return schedule

    async def async_delete_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a schedule by id."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
        schedule_id = str(payload.get("id") or payload.get("schedule_id") or "").strip()
        if not schedule_id:
            raise ValueError("schedule id is required")
        before = len(self.schedules())
        self._storage["schedules"] = [
            schedule
            for schedule in self.schedules()
            if not (schedule.get("profile_id") == profile_id and schedule.get("id") == schedule_id)
        ]
        removed = before - len(self.schedules())
        await self.async_save()
        self._reschedule_all_schedules()
        self._stats_cache = None
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        await self.async_record_activity(
            "schedule_deleted",
            f"Schedule deleted: {schedule_id}",
            profile_id=profile_id,
            data={"schedule_id": schedule_id, "removed": removed},
        )
        return {"profile_id": profile_id, "schedule_id": schedule_id, "removed": removed}

    async def async_run_due_schedules(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run enabled schedules that are due for the current minute."""
        return await self._schedule_manager.async_run_due(now, trigger=self._last_tick_trigger or "interval")

    async def async_run_scheduled_schedule(
        self,
        profile_id: str,
        schedule_id: str,
        due_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Run one exact point-in-time schedule job."""
        return await self._schedule_manager.async_run_schedule(
            profile_id,
            schedule_id,
            due_at,
            trigger="point_in_time",
        )

    def _schedule_due(self, schedule: dict[str, Any], now: datetime) -> bool:
        """Return whether a schedule should run for this local minute."""
        return _due_schedule_datetime(schedule, now) is not None

    def _schedule_run_key(self, schedule: dict[str, Any], now: datetime) -> str:
        """Return a per-minute run key for a schedule."""
        return (
            f"{schedule.get('profile_id')}:{schedule.get('id')}:"
            f"{now.date().isoformat()}:{schedule.get('time')}"
        )

    async def async_execute_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        """Execute a stored playback schedule."""
        player = str(schedule.get("player") or "").strip()
        media_id = str(schedule.get("media_id") or "").strip()
        media_type = str(schedule.get("media_type") or "music").strip() or "music"
        player_before = _player_playback_snapshot(self.hass.states.get(player)) if player else {}
        resolution: dict[str, Any] = {
            "ok": bool(media_id),
            "mode": _schedule_media_mode(schedule.get("media_mode"), media_id),
            "media_id": media_id,
            "media_type": media_type,
            "media_name": schedule.get("media_name") or schedule.get("playlist_name") or "",
            "provider": "stored",
        }
        if player and not media_id:
            resolution = await self.async_resolve_schedule_media(schedule, media_type)
            media_id = str(resolution.get("media_id") or "").strip()
            media_type = str(resolution.get("media_type") or media_type or "playlist").strip() or "playlist"
        if not player:
            return {
                "ok": False,
                "schedule_id": schedule.get("id"),
                "phase": "validate",
                "error": "player is required",
                "resolution": resolution,
                "executed_at": _utc_iso(),
            }
        volume = schedule.get("volume")
        volume_error = ""
        if volume is not None:
            try:
                await self.async_call_service_response(
                    "media_player",
                    "volume_set",
                    {"entity_id": player, "volume_level": max(0, min(100, int(volume))) / 100},
                    return_response=False,
                )
            except Exception as err:  # noqa: BLE001 - volume must not block wake playback
                volume_error = str(err)
        play_result: dict[str, Any] = {}
        play_error = ""
        verified = False
        schedule_attempts: list[dict[str, Any]] = []
        max_attempts = _bounded_int(schedule.get("retry_attempts"), 4, 1, 12)
        retry_delay = _bounded_int(schedule.get("retry_delay"), 5, 1, 30)
        if media_id:
            for attempt_index in range(max_attempts):
                readiness = self._player_readiness(player)
                attempt_record: dict[str, Any] = {
                    "attempt": attempt_index + 1,
                    "at": _utc_iso(),
                    "readiness": readiness,
                }
                if not readiness.get("ready"):
                    play_error = str(readiness.get("reason") or "player is not ready")
                    attempt_record["ok"] = False
                    attempt_record["error"] = play_error
                    schedule_attempts.append(attempt_record)
                else:
                    try:
                        play_result = await self.async_play_media(
                            {
                                "player": player,
                                "media_id": media_id,
                                "media_type": media_type,
                                "enqueue": schedule.get("enqueue") or "play",
                                "radio_mode": bool(schedule.get("radio_mode")),
                                "verify_playback": True,
                            }
                        )
                        verified = bool(play_result.get("verified", True))
                        attempt_record["ok"] = verified
                        attempt_record["provider"] = play_result.get("provider")
                        attempt_record["playback_attempts"] = play_result.get("attempts") or []
                        if not verified:
                            play_error = "Playback service was called, but the player did not confirm a change."
                            attempt_record["error"] = play_error
                        schedule_attempts.append(attempt_record)
                    except Exception as err:  # noqa: BLE001 - every failed attempt is reported
                        play_error = str(err)
                        attempt_record["ok"] = False
                        attempt_record["error"] = play_error
                        schedule_attempts.append(attempt_record)
                if verified:
                    break
                if attempt_index < max_attempts - 1:
                    await asyncio.sleep(retry_delay)
                    if play_result:
                        delayed_after = _player_playback_snapshot(self.hass.states.get(player))
                        if _playback_snapshot_changed(player_before, delayed_after):
                            verified = True
                            attempt_record["delayed_verified"] = True
                            attempt_record["delayed_player_after"] = delayed_after
                            play_result["player_after"] = delayed_after
                            break
        else:
            play_error = str(resolution.get("error") or "No playable media was resolved for this schedule.")

        provider = play_result.get("provider") or ""
        error = ""
        if not verified:
            error_parts = [
                part
                for part in (
                    play_error,
                    "Playback service was called, but the target player did not report playback/media/queue changes."
                    if play_result
                    else "",
                )
                if part
            ]
            error = "; ".join(error_parts) or "Schedule playback did not start."
        result = {
            "ok": verified,
            "schedule_id": schedule.get("id"),
            "phase": "playback" if media_id else "resolve",
            "player": player,
            "media_id": media_id,
            "media_type": media_type,
            "media_name": resolution.get("media_name") or schedule.get("media_name") or schedule.get("playlist_name") or "",
            "media_mode": resolution.get("mode") or schedule.get("media_mode"),
            "provider": provider,
            "verified": verified,
            "attempts": play_result.get("attempts") or [],
            "schedule_attempts": schedule_attempts,
            "retry_attempts": max_attempts,
            "retry_delay": retry_delay,
            "resolution": resolution,
            "volume_error": volume_error,
            "player_before": play_result.get("player_before") or player_before,
            "player_after": play_result.get("player_after") or _player_playback_snapshot(self.hass.states.get(player)),
            "error": error,
            "executed_at": _utc_iso(),
        }
        await self.async_record_activity(
            "schedule_executed" if verified else "schedule_failed",
            f"Schedule {'executed' if verified else 'failed'}: {schedule.get('name') or schedule.get('id')}",
            profile_id=str(schedule.get("profile_id") or DEFAULT_PROFILE_ID),
            data={
                "schedule_id": schedule.get("id"),
                "player": player,
                "media_id": media_id,
                "media_name": result.get("media_name"),
                "ok": verified,
                "error": error,
            },
        )
        return result

    async def async_run_schedule_now(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a stored schedule immediately for diagnostics and manual actions."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
        schedule_id = str(payload.get("id") or payload.get("schedule_id") or "").strip()
        if not schedule_id:
            raise ValueError("schedule id is required")
        schedule = next(
            (
                item
                for item in self.schedules(profile_id)
                if str(item.get("id") or item.get("schedule_id") or "").strip() == schedule_id
            ),
            None,
        )
        if schedule is None:
            raise ValueError(f"schedule not found: {schedule_id}")
        try:
            result = await self.async_execute_schedule(schedule)
        except Exception as err:  # noqa: BLE001 - surfaced through service/websocket result
            result = {
                "ok": False,
                "schedule_id": schedule.get("id"),
                "player": schedule.get("player"),
                "media_id": schedule.get("media_id"),
                "media_type": schedule.get("media_type"),
                "error": str(err),
                "executed_at": _utc_iso(),
            }
        self._last_schedule_action = result
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        return result

    async def async_resolve_schedule_media(
        self,
        schedule: dict[str, Any],
        fallback_media_type: str = "playlist",
    ) -> dict[str, Any]:
        """Resolve a playable media item for schedules without an explicit media id."""
        media_type = str(schedule.get("media_type") or fallback_media_type or "playlist").strip() or "playlist"
        if media_type == "music":
            media_type = "playlist"
        media_mode = _schedule_media_mode(schedule.get("media_mode"), str(schedule.get("media_id") or ""))
        try:
            response = await self.async_get_library(
                {"media_type": media_type, "limit": 250, "compact": True}
            )
        except Exception as err:  # noqa: BLE001 - returned for diagnostics
            return {
                "ok": False,
                "mode": media_mode,
                "media_id": "",
                "media_type": media_type,
                "media_name": "",
                "provider": "homeii_flow_engine.library",
                "error": str(err),
            }
        items = _safe_list(response.get("items")) if isinstance(response, dict) else []
        playable = [item for item in items if _media_item_id(item)]
        if not playable:
            return {
                "ok": False,
                "mode": media_mode,
                "media_id": "",
                "media_type": media_type,
                "media_name": "",
                "provider": "homeii_flow_engine.library",
                "candidate_count": 0,
                "error": "Music Assistant returned no playable library items.",
            }
        if media_mode == "random_playlist":
            scored = [item for item in playable if _schedule_morning_score(item) > 0]
            pool = scored or playable
        else:
            pool = playable
        seed = f"{schedule.get('profile_id')}:{schedule.get('id')}:{_local_datetime().date().isoformat()}"
        index = sum(ord(char) for char in seed) % len(pool)
        item = pool[index]
        return {
            "ok": True,
            "mode": media_mode,
            "media_id": _media_item_id(item),
            "media_type": _media_item_type(item, media_type),
            "media_name": _media_item_name(item),
            "provider": "homeii_flow_engine.library",
            "candidate_count": len(playable),
            "preferred_candidate_count": len(pool),
            "score": _schedule_morning_score(item),
        }

    async def async_set_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store or update a one-shot timer."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
        player = str(payload.get("player") or payload.get("entity_id") or "").strip()
        if not player:
            raise ValueError("player is required")

        ends_at = _parse_utc_datetime(payload.get("ends_at") or payload.get("target_at"))
        if ends_at is None:
            minutes = max(1, int(payload.get("minutes") or 0))
            ends_at = datetime.now(UTC) + timedelta(minutes=minutes)
        else:
            minutes = max(1, int((ends_at - datetime.now(UTC)).total_seconds() // 60))

        timer_id = (
            str(payload.get("id") or payload.get("timer_id") or "").strip()
            or f"sleep_{_safe_id_part(profile_id, 'default')}_{_safe_id_part(player, 'player')}"
        )
        existing = next(
            (
                item
                for item in self.timers()
                if item.get("profile_id") == profile_id and item.get("id") == timer_id
            ),
            {},
        )
        action = str(payload.get("action") or "stop").strip().lower()
        if action not in {"stop", "pause"}:
            action = "stop"
        timer = {
            "id": timer_id,
            "profile_id": profile_id,
            "type": str(payload.get("timer_type") or payload.get("kind") or "sleep").strip() or "sleep",
            "player": player,
            "action": action,
            "minutes": minutes,
            "ends_at": ends_at.isoformat(),
            "enabled": bool(payload.get("enabled", True)),
            "origin": str(payload.get("origin") or "").strip(),
            "created_at": existing.get("created_at") or _utc_iso(),
            "updated_at": _utc_iso(),
        }
        timers = [
            existing_timer
            for existing_timer in self.timers()
            if not (existing_timer.get("profile_id") == profile_id and existing_timer.get("id") == timer_id)
        ]
        timers.append(timer)
        self._storage["timers"] = timers
        self._stats_cache = None
        await self.async_save()
        await self.async_record_activity(
            "timer_saved",
            f"Timer saved for {player}",
            profile_id=profile_id,
            data={"timer_id": timer_id, "player": player, "action": action, "ends_at": ends_at.isoformat()},
        )
        return timer

    async def async_delete_timer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a one-shot timer by id or player."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
        timer_id = str(payload.get("id") or payload.get("timer_id") or "").strip()
        player = str(payload.get("player") or payload.get("entity_id") or "").strip()
        if not timer_id and not player:
            raise ValueError("timer id or player is required")
        before = len(self.timers())
        self._storage["timers"] = [
            timer
            for timer in self.timers()
            if not (
                str(timer.get("profile_id") or DEFAULT_PROFILE_ID) == profile_id
                and ((timer_id and timer.get("id") == timer_id) or (not timer_id and timer.get("player") == player))
            )
        ]
        removed = before - len(self.timers())
        self._stats_cache = None
        await self.async_save()
        await self.async_record_activity(
            "timer_deleted",
            f"Timer deleted: {timer_id or player}",
            profile_id=profile_id,
            data={"timer_id": timer_id, "player": player, "removed": removed},
        )
        return {"profile_id": profile_id, "timer_id": timer_id, "player": player, "removed": removed}

    async def async_run_due_timers(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run enabled one-shot timers that are due."""
        current = now or dt_util.now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        current_utc = current.astimezone(UTC)
        results: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        changed = False
        for timer in self.timers():
            if not bool(timer.get("enabled", True)):
                remaining.append(timer)
                continue
            ends_at = _parse_utc_datetime(timer.get("ends_at"))
            if ends_at is None or ends_at > current_utc:
                remaining.append(timer)
                continue
            try:
                result = await self.async_execute_timer(timer)
            except Exception as err:  # noqa: BLE001 - keep orchestration alive and expose failure
                result = {
                    "ok": False,
                    "timer_id": timer.get("id"),
                    "player": timer.get("player"),
                    "action": timer.get("action"),
                    "error": str(err),
                    "executed_at": _utc_iso(),
                }
            self._last_timer_action = result
            results.append(result)
            changed = True
        if changed:
            self._storage["timers"] = remaining
            await self.async_save()
        return results

    async def async_execute_timer(self, timer: dict[str, Any]) -> dict[str, Any]:
        """Share one execution across point-in-time and catch-up runners."""
        key = (str(timer.get("profile_id") or DEFAULT_PROFILE_ID),
               str(timer.get("id") or ""), str(timer.get("ends_at") or ""))
        task = self._timer_execution_tasks.get(key)
        if task is None:
            # Claim before yielding: activity updates can re-enter this method.
            task = self.hass.async_create_task(self._async_execute_timer_once(dict(timer)))
            self._timer_execution_tasks[key] = task
            if len(self._timer_execution_tasks) > 128:
                for old_key, old_task in list(self._timer_execution_tasks.items()):
                    if old_key != key and old_task.done():
                        del self._timer_execution_tasks[old_key]
                        if len(self._timer_execution_tasks) <= 128:
                            break
        return dict(await asyncio.shield(task))

    async def _async_execute_timer_once(self, timer: dict[str, Any]) -> dict[str, Any]:
        """Execute a stored one-shot timer."""
        player = str(timer.get("player") or "").strip()
        action = str(timer.get("action") or "stop").strip().lower()
        if not player:
            return {"ok": False, "timer_id": timer.get("id"), "error": "player is required"}
        service = "media_pause" if action == "pause" else "media_stop"
        await self.async_call_service_response(
            "media_player",
            service,
            {"entity_id": player},
            return_response=False,
        )
        result = {
            "ok": True,
            "timer_id": timer.get("id"),
            "player": player,
            "action": action,
            "provider": f"media_player.{service}",
            "executed_at": _utc_iso(),
        }
        await self.async_record_activity(
            "timer_executed",
            f"Timer executed for {player}",
            profile_id=str(timer.get("profile_id") or DEFAULT_PROFILE_ID),
            data=result,
        )
        return result

    async def async_call_service_response(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        *,
        return_response: bool = True,
        target: dict[str, Any] | None = None,
    ) -> Any:
        """Call a Home Assistant service and optionally return response data."""
        if not self.hass.services.has_service(domain, service):
            raise HomeiiFlowServiceUnavailable(f"{domain}.{service} is not available")
        original_data = dict(service_data or {})
        data, entry_id = self._music_assistant_service_payload(domain, original_data)
        clean_target = dict(target or {})

        async def call_once(call_data: dict[str, Any]) -> Any:
            if not return_response:
                try:
                    await self.hass.services.async_call(
                        domain,
                        service,
                        call_data,
                        blocking=True,
                        target=clean_target or None,
                    )
                except TypeError:
                    retry_data = dict(call_data)
                    if clean_target.get("entity_id") and "entity_id" not in retry_data:
                        retry_data["entity_id"] = clean_target["entity_id"]
                    await self.hass.services.async_call(domain, service, retry_data, blocking=True)
                return {
                    "called": True,
                    "return_response": False,
                    "config_entry_id": entry_id,
                    "config_entry_id_used": bool(entry_id and call_data.get("config_entry_id") == entry_id),
                }
            try:
                return await self.hass.services.async_call(
                    domain,
                    service,
                    call_data,
                    blocking=True,
                    target=clean_target or None,
                    return_response=True,
                )
            except TypeError:
                retry_data = dict(call_data)
                if clean_target.get("entity_id") and "entity_id" not in retry_data:
                    retry_data["entity_id"] = clean_target["entity_id"]
                await self.hass.services.async_call(domain, service, retry_data, blocking=True)
                return {
                    "called": True,
                    "return_response": False,
                    "config_entry_id": entry_id,
                    "config_entry_id_used": bool(entry_id and retry_data.get("config_entry_id") == entry_id),
                }

        if not return_response:
            try:
                return await call_once(data)
            except Exception as err:
                if entry_id and self._service_rejected_config_entry_id(err):
                    result = await call_once(original_data)
                    if isinstance(result, dict):
                        result["config_entry_id_retry_without"] = True
                    return result
                raise
        try:
            return await call_once(data)
        except Exception as err:
            if entry_id and self._service_rejected_config_entry_id(err):
                return await call_once(original_data)
            raise

    def _music_assistant_command_base_urls(self, payload: dict[str, Any]) -> list[str]:
        """Return candidate Music Assistant API base URLs for server-side commands."""
        urls: list[str] = []
        preferred = _normalized_http_url(self._ma_preferred_base_url)
        if preferred:
            urls.append(preferred)
        for candidate in self.music_assistant_base_urls():
            clean = _normalized_http_url(candidate)
            if clean and clean not in urls:
                urls.append(clean)
        return urls

    async def _async_music_assistant_server_info(self) -> dict[str, Any]:
        """Read and validate the MA 2.10 HTTP server-info contract."""
        urls = self._music_assistant_command_base_urls({})
        if not urls:
            raise HomeiiFlowServiceUnavailable("No Music Assistant URL is configured.")
        session = async_get_clientsession(self.hass)
        errors: list[str] = []
        for base_url in urls:
            try:
                async with session.get(
                    f"{base_url.rstrip('/')}/info",
                    headers={"Accept": "application/json"},
                    timeout=ClientTimeout(total=8, connect=4),
                ) as response:
                    if response.status >= 400:
                        errors.append(f"{base_url}: HTTP {response.status}")
                        continue
                    info = await response.json(content_type=None)
                if not isinstance(info, dict):
                    errors.append(f"{base_url}: invalid server info")
                    continue
                schema_value = _dict_first(
                    info,
                    "schema_version",
                    "api_schema_version",
                    "api_schema",
                )
                try:
                    schema_version = int(schema_value)
                except (TypeError, ValueError):
                    schema_version = 0
                if schema_version < MUSIC_ASSISTANT_SCHEMA_MIN:
                    errors.append(
                        f"{base_url}: API schema {MUSIC_ASSISTANT_SCHEMA_MIN} or newer is required; "
                        f"server reported {schema_version or 'unknown'}"
                    )
                    continue
                self._ma_preferred_base_url = base_url
                self._ma_http_health.update(
                    {
                        "connected": True,
                        "schema_version": schema_version,
                        "schema_supported": True,
                        "server_version": _clean_string(
                            _dict_first(info, "server_version", "version")
                        ),
                        "server_id": _clean_string(info.get("server_id")),
                        "info_checked_at": _utc_iso(),
                        "last_error": "",
                    }
                )
                return info
            except (ClientError, TimeoutError, ValueError, TypeError) as err:
                errors.append(f"{base_url}: {err}")
        error_message = "; ".join(errors) or "Music Assistant server info is unavailable."
        self._ma_http_health.update(
            {
                "connected": False,
                "schema_supported": False,
                "last_error": error_message[:500],
            }
        )
        raise HomeiiFlowServiceUnavailable(error_message)

    async def _async_music_assistant_contract_probe(
        self,
        player_snapshot: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Verify the read contracts required by the card instead of inferring health."""
        now = time.monotonic()
        if not force and self._ma_contract_checks and now - self._ma_contract_probe_at < 60:
            return copy.deepcopy(self._ma_contract_checks)
        players = [
            player
            for player in _safe_list((player_snapshot or {}).get("music_assistant_players"))
            if isinstance(player, dict)
        ]
        player = next(
            (
                candidate
                for candidate in players
                if candidate.get("available", candidate.get("attributes", {}).get("available", True)) is not False
                and str(candidate.get("state") or "").lower() not in {"unavailable", "unknown"}
            ),
            players[0] if players else {},
        )
        player_id = _clean_string(
            _dict_first(player, "mass_player_id", "player_id", "entity_id")
        )
        probes: dict[str, tuple[str, dict[str, Any]]] = {
            "library": (
                "music/playlists/library_items",
                {"limit": 1, "offset": 0, "order_by": "sort_name"},
            ),
            "search": (
                "music/search",
                {
                    "search_query": "homeii_contract_probe_no_match",
                    "media_types": ["track"],
                    "limit": 1,
                    "providers": ["library"],
                },
            ),
        }
        async def run_probe(name: str, command: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            started = time.perf_counter()
            try:
                await self.async_music_assistant_command({"command": command, "args": args})
                return name, {
                    "ok": True,
                    "command": command,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "checked_at": _utc_iso(),
                    "error": "",
                }
            except Exception as err:  # noqa: BLE001 - every failed scope must remain visible
                return name, {
                    "ok": False,
                    "command": command,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "checked_at": _utc_iso(),
                    "error": str(err)[:500],
                }

        async def run_queue_probe() -> tuple[str, dict[str, Any]]:
            """Verify both queue identity and the full queue-items read contract."""
            started = time.perf_counter()
            try:
                active_response = await self.async_music_assistant_command(
                    {
                        "command": "player_queues/get_active_queue",
                        "args": {"player_id": player_id},
                    }
                )
                active_queue = (
                    active_response.get("data")
                    if isinstance(active_response, dict)
                    else active_response
                )
                queue_id = _clean_string(
                    _dict_first(active_queue, "queue_id", "player_id")
                    if isinstance(active_queue, dict)
                    else ""
                ) or player_id
                items_response = await self.async_music_assistant_command(
                    {
                        "command": "player_queues/items",
                        "args": {"queue_id": queue_id, "limit": 1, "offset": 0},
                    }
                )
                items = items_response.get("data") if isinstance(items_response, dict) else items_response
                if not isinstance(items, list):
                    raise HomeiiFlowServiceUnavailable(
                        "Music Assistant player_queues/items returned an invalid response."
                    )
                return "queue", {
                    "ok": True,
                    "command": "player_queues/get_active_queue+items",
                    "queue_id": queue_id,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "checked_at": _utc_iso(),
                    "error": "",
                }
            except Exception as err:  # noqa: BLE001 - queue readiness must remain visible
                return "queue", {
                    "ok": False,
                    "command": "player_queues/get_active_queue+items",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "checked_at": _utc_iso(),
                    "error": str(err)[:500],
                }

        probe_tasks = [
            run_probe(name, command, args)
            for name, (command, args) in probes.items()
        ]
        if player_id:
            probe_tasks.append(run_queue_probe())
        results = await asyncio.gather(*probe_tasks)
        checks = dict(results)
        if "queue" not in checks:
            checks["queue"] = {
                "ok": False,
                "command": "player_queues/get_active_queue",
                "latency_ms": 0,
                "checked_at": _utc_iso(),
                "error": "Music Assistant returned no enabled player for the queue probe.",
            }
        self._ma_contract_checks = checks
        self._ma_contract_probe_at = now
        return copy.deepcopy(checks)

    async def async_queue_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Expose supported queue settings without opening the general config API."""
        status = self._music_assistant_client.snapshot()
        if not status.get("authenticated") or not status.get("schema_supported"):
            raise HomeiiFlowServiceUnavailable("Music Assistant is not ready")
        return await async_queue_settings(self._music_assistant_client, payload.get("values"))

    @staticmethod
    def _music_assistant_command_allowed(command: str) -> bool:
        """Allow card playback/media commands while blocking account and server administration."""
        clean = str(command or "").strip().lower()
        if clean == "info":
            return True
        if clean == "music/playlists/add_playlist_tracks":
            return True
        if clean.startswith("ai_radio/"):
            return clean in {"ai_radio/hosts/list", "ai_radio/queue_dj/status", "ai_radio/queue_dj/set"}
        if not clean.startswith(
            (
                "players/",
                "player_queues/",
                "music/",
                "metadata/",
                "audio_analysis/",
            )
        ):
            return False
        if clean.startswith("music/favorites/"):
            return clean in {"music/favorites/add_item", "music/favorites/remove_item"}
        if not clean.startswith("music/"):
            return True
        blocked_mutations = (
            "/create",
            "/update",
            "/delete",
            "/remove",
            "/import",
            "/export",
            "/sync",
            "/add_playlist",
            "/remove_playlist",
        )
        return not any(token in clean for token in blocked_mutations)

    @staticmethod
    def _music_assistant_command_cacheable(command: str) -> bool:
        """Return whether a read-only media command benefits from persistent SWR."""
        return command.strip().lower() in {
            "music/item_by_uri",
            "music/albums/album_tracks",
            "music/playlists/playlist_tracks",
            "music/artists/artist_albums",
            "music/artists/artist_tracks",
            "music/recommendations",
            "music/tracks/similar_tracks",
            "music/recently_played_items",
            "music/in_progress_items",
        }

    @staticmethod
    def _music_assistant_command_cache_key(command: str, args: dict[str, Any]) -> str:
        """Return a stable, privacy-safe key for a media detail request."""
        signature = repr((command.strip().lower(), sorted(args.items(), key=lambda item: item[0])))
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()

    def _finish_media_command_refresh(
        self,
        cache_key: str,
        task: asyncio.Task[dict[str, Any]],
    ) -> None:
        """Consume a background refresh result and release its singleflight slot."""
        if self._media_command_inflight.get(cache_key) is task:
            self._media_command_inflight.pop(cache_key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - stale data remains valid after refresh failure
            self._media_cache_metrics["refresh_failures"] += 1

    async def async_music_assistant_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a Music Assistant API command from the Engine server context."""
        command = str(payload.get("command") or "").strip()
        args = payload.get("args")
        if not command:
            raise ValueError("command is required")
        if not self._music_assistant_command_allowed(command):
            raise ValueError(f"Music Assistant command is not allowed through the card bridge: {command}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError("args must be a dictionary")
        cacheable = self._music_assistant_command_cacheable(command)
        cache_key = self._music_assistant_command_cache_key(command, args) if cacheable else ""
        cache_worker = bool(payload.get("_homeii_cache_worker"))
        cache_refresh = bool(payload.get("_homeii_cache_refresh"))
        if cacheable and not cache_worker:
            now = time.monotonic()
            cached = self._media_command_cache.get(cache_key)
            if cached and not cache_refresh:
                if float(cached.get("fresh_until") or 0) > now:
                    self._media_cache_metrics["command_hits"] = int(
                        self._media_cache_metrics.get("command_hits") or 0
                    ) + 1
                    if cached.get("persistent"):
                        self._media_cache_metrics["persistent_hits"] += 1
                    return copy.deepcopy(cached["result"])
                if float(cached.get("stale_until") or 0) > now:
                    self._media_cache_metrics["command_stale_hits"] = int(
                        self._media_cache_metrics.get("command_stale_hits") or 0
                    ) + 1
                    if cache_key not in self._media_command_inflight:
                        task = self.hass.async_create_task(
                            self.async_music_assistant_command(
                                {
                                    **payload,
                                    "_homeii_cache_worker": True,
                                    "_homeii_cache_refresh": True,
                                }
                            )
                        )
                        self._media_command_inflight[cache_key] = task
                        task.add_done_callback(
                            lambda completed, key=cache_key: self._finish_media_command_refresh(key, completed)
                        )
                    return copy.deepcopy(cached["result"])
            inflight = self._media_command_inflight.get(cache_key)
            if inflight is not None:
                self._media_cache_metrics["coalesced"] += 1
                return copy.deepcopy(await asyncio.shield(inflight))
            self._media_cache_metrics["command_misses"] = int(
                self._media_cache_metrics.get("command_misses") or 0
            ) + 1
            task = self.hass.async_create_task(
                self.async_music_assistant_command(
                    {
                        **payload,
                        "_homeii_cache_worker": True,
                        "_homeii_cache_refresh": cache_refresh,
                    }
                )
            )
            self._media_command_inflight[cache_key] = task
            try:
                return copy.deepcopy(await asyncio.shield(task))
            finally:
                if self._media_command_inflight.get(cache_key) is task:
                    self._media_command_inflight.pop(cache_key, None)
        realtime = self._music_assistant_client.snapshot()
        if not realtime.get("authenticated") or not realtime.get("schema_supported"):
            raise HomeiiFlowServiceUnavailable(
                "The authenticated Music Assistant WebSocket command channel is not ready."
            )
        try:
            result = await self._music_assistant_client.async_command(
                command,
                args,
                timeout=max(10.0, min(60.0, float(payload.get("timeout") or 30))),
            )
        except (RuntimeError, TimeoutError, ValueError) as err:
            error_message = str(err) or f"{command} failed"
            self._ma_http_health.update(
                {
                    "connected": bool(realtime.get("connected")),
                    "authenticated": bool(realtime.get("authenticated")),
                    "last_error": error_message[:500],
                    "last_command": command,
                }
            )
            raise HomeiiFlowServiceUnavailable(error_message) from err
        output = {
            "provider": "music_assistant.authenticated_websocket",
            "source_of_truth": "homeii_flow_engine",
            "command": command,
            "authenticated": True,
            "data": self.decorate_artwork_urls(result),
        }
        self._ma_http_health.update(
            {
                "connected": True,
                "authenticated": True,
                "last_success_at": _utc_iso(),
                "last_error": "",
                "last_command": command,
            }
        )
        if cacheable:
            now = time.monotonic()
            self._media_command_cache[cache_key] = {
                "fresh_until": now + 15 * 60,
                "stale_until": now + 24 * 60 * 60,
                "stored_at": time.time(),
                "result": copy.deepcopy(output),
                "persistent": False,
                "invalidated": False,
            }
            if len(self._media_command_cache) > 80:
                newest = sorted(
                    self._media_command_cache.items(),
                    key=lambda item: float(item[1].get("stored_at") or 0),
                    reverse=True,
                )[:64]
                self._media_command_cache = dict(newest)
            self._schedule_media_cache_save()
        elif command.strip().lower().startswith("music/favorites/"):
            self._mark_library_cache_stale()
            self._mark_media_command_cache_stale()
        return output

    @staticmethod
    def _unique_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return dictionaries once while preserving order."""
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for payload in payloads:
            clean = {key: value for key, value in dict(payload or {}).items() if value not in (None, "")}
            marker = repr(sorted(clean.items(), key=lambda item: item[0]))
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(clean)
        return unique

    @staticmethod
    def _media_type_command_roots(media_type: str) -> list[str]:
        """Return likely Music Assistant command path roots for a media type."""
        clean_type = _clean_string(media_type).lower() or "playlist"
        roots_by_type = {
            "album": ["albums"],
            "artist": ["artists"],
            "audiobook": ["audiobooks"],
            "genre": ["genres"],
            "playlist": ["playlists"],
            "podcast": ["podcasts"],
            "radio": ["radios"],
            "track": ["tracks"],
        }
        return roots_by_type.get(clean_type, [])

    def _music_library_command_attempts(
        self,
        *,
        media_type: str,
        limit: int,
        offset: int = 0,
        order_by: str = "",
        query: str = "",
        favorite_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the exact MA 2.10 library command for one media type."""
        base_payload: dict[str, Any] = {"limit": limit, "offset": offset}
        if order_by:
            base_payload["order_by"] = order_by
        if favorite_only:
            base_payload["favorite"] = True
        if query:
            base_payload["search"] = query
        roots = self._media_type_command_roots(media_type)
        if not roots:
            return []
        root = roots[0]
        return [{"command": f"music/{root}/library_items", "payloads": [base_payload]}]

    def _normalize_library_command_items(
        self,
        response: Any,
        *,
        media_type: str,
    ) -> list[dict[str, Any]]:
        """Normalize MA API library command output into card-ready items."""
        decorated = self.decorate_artwork_urls(response)
        items: list[dict[str, Any]] = []
        for index, raw_item in enumerate(_extract_media_items(decorated)):
            normalized_item = self.normalize_media_item(raw_item, fallback_media_type=media_type, fallback_index=index)
            if normalized_item:
                items.append(normalized_item)
        return items

    async def _try_music_library_command_bridge(
        self,
        *,
        media_type: str,
        limit: int,
        offset: int = 0,
        order_by: str = "",
        query: str = "",
        favorite_only: bool = False,
        profile_id: str = "",
    ) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        """Try Music Assistant server API as the Engine-owned library backend."""
        attempts: list[dict[str, Any]] = []
        best_response: Any = None
        best_items: list[dict[str, Any]] = []
        bridge_attempts = self._music_library_command_attempts(
            media_type=media_type,
            limit=limit,
            offset=offset,
            order_by=order_by,
            query=query,
            favorite_only=favorite_only,
        )
        for command_attempt in bridge_attempts:
            command = command_attempt["command"]
            for args in command_attempt["payloads"]:
                try:
                    response = await self.async_music_assistant_command(
                        {
                            "command": command,
                            "args": args,
                            "profile_id": profile_id,
                        }
                    )
                    data = response.get("data") if isinstance(response, dict) else response
                    items = self._normalize_library_command_items(data, media_type=media_type)
                    attempts.append(
                        {
                            "provider": "music_assistant.server_command",
                            "command": command,
                            "data": args,
                            "ok": True,
                            "items": len(items),
                        }
                    )
                    if best_response is None or len(items) > len(best_items):
                        best_response = data
                        best_items = items
                    if items:
                        return best_response, best_items, attempts
                except Exception as err:  # noqa: BLE001 - command availability differs across MA versions
                    attempts.append(
                        {
                            "provider": "music_assistant.server_command",
                            "command": command,
                            "data": args,
                            "error": str(err),
                        }
                    )
        return best_response, best_items, attempts

    async def _music_assistant_provider_ids(self) -> list[str]:
        """Return searchable MA provider instance ids from the 2.10 browse root."""
        expires_at, cached = self._provider_ids_cache
        if cached and expires_at > time.monotonic():
            return list(cached)
        response = await self.async_music_assistant_command(
            {"command": "music/browse", "args": {"path": "root"}}
        )
        data = response.get("data") if isinstance(response, dict) else response
        providers: list[str] = []
        for item in _safe_list(data):
            if not isinstance(item, dict):
                continue
            path = _clean_string(_dict_first(item, "path", "uri"))
            provider = path.split("://", 1)[0] if "://" in path else _clean_string(
                _dict_first(item, "provider_instance", "provider")
            )
            if provider and provider != "library" and provider not in providers:
                providers.append(provider)
        self._provider_ids_cache = (time.monotonic() + 15 * 60, list(providers))
        return providers

    async def _try_music_queue_command_bridge(
        self,
        *,
        entity_id: str,
        queue_id: str,
        player: dict[str, Any] | None,
        limit_before: int,
        limit_after: int,
        profile_id: str = "",
    ) -> list[dict[str, Any]]:
        """Try Music Assistant server API as the Engine-owned queue backend."""
        results: list[dict[str, Any]] = []
        request_revision = self._snapshot_revisions.get("queue", 0)
        # Resolve on every read: a supplied queue id may predate grouping or transfer.
        player_id = self._resolve_ma_player_id(entity_id)
        response = await self.async_music_assistant_command({
            "command": "player_queues/get_active_queue",
            "args": {"player_id": player_id}, "profile_id": profile_id,
        })
        active_queue = response.get("data") if isinstance(response, dict) else response
        resolved_queue_id = _clean_string(active_queue.get("queue_id")) if isinstance(active_queue, dict) else ""
        resolved_queue_id = resolved_queue_id or player_id
        provider = "music_assistant.server_command:player_queues/get+items"
        response = await self.async_music_assistant_command({
            "command": "player_queues/get", "args": {"queue_id": resolved_queue_id},
            "profile_id": profile_id,
        })
        queue_state = response.get("data") if isinstance(response, dict) else response
        if queue_state is None and active_queue is None:
            queue_state = {"queue_id": resolved_queue_id, "items": 0, "current_index": None, "active": False}
        if not isinstance(queue_state, dict):
            raise HomeiiFlowServiceUnavailable("Music Assistant returned an invalid queue state.")
        expected_count = int(queue_state.get("items") or 0)
        queue_items: list[Any] = []
        # A full snapshot must include tracks past the first 500 items.
        while len(queue_items) < expected_count:
            response = await self.async_music_assistant_command({
                "command": "player_queues/items",
                "args": {"queue_id": resolved_queue_id, "limit": 500, "offset": len(queue_items)},
                "profile_id": profile_id,
            })
            page = response.get("data") if isinstance(response, dict) else response
            if not isinstance(page, list) or not page:
                raise HomeiiFlowServiceUnavailable("Music Assistant returned an incomplete queue; refresh required.")
            queue_items.extend(page)
        if expected_count:
            response = await self.async_music_assistant_command({
                "command": "player_queues/get", "args": {"queue_id": resolved_queue_id},
                "profile_id": profile_id,
            })
            latest = response.get("data") if isinstance(response, dict) else response
            if not isinstance(latest, dict) or latest.get("items") != queue_state.get("items"):
                raise HomeiiFlowServiceUnavailable("Music Assistant queue changed while loading; refresh required.")
            queue_state = latest
        if self._snapshot_revisions.get("queue", 0) != request_revision:
            raise HomeiiFlowServiceUnavailable("Music Assistant queue changed while loading; refresh required.")
        combined = dict(queue_state)
        combined["queue_id"] = resolved_queue_id
        combined["items_count"] = expected_count
        combined["items"] = queue_items
        decorated = self.decorate_artwork_urls(combined)
        normalized = self.normalize_queue_response(
            decorated,
            entity_id=entity_id,
            queue_id=resolved_queue_id,
            player=player,
        )
        visible = int(normalized.get("visible_items") or len(normalized.get("items") or []))
        expected = int(normalized.get("items_count") or self._queue_payload_expected_count(decorated))
        proxy_art = sum(
            1 for item in normalized.get("items", []) if _clean_string(item.get("homeii_artwork_url"))
        )
        results.append(
            {
                "provider": provider,
                "response": decorated,
                "normalized": normalized,
                "visible_items": visible,
                "expected_items": expected,
                "proxy_artwork_items": proxy_art,
                "complete": visible == expected,
                "score": (
                    1 if visible and (not expected or visible >= expected) else 0,
                    visible,
                    proxy_art,
                    min(expected, visible) if expected else visible,
                    4,
                ),
            }
        )
        return results

    @staticmethod
    def _queue_payload_root(value: Any, entity_id: str = "") -> Any:
        """Return the most likely queue payload root."""
        if not isinstance(value, dict):
            return value
        clean_entity = _clean_string(entity_id)
        if clean_entity and isinstance(value.get(clean_entity), dict):
            return value[clean_entity]
        for key in ("response", "data", "result"):
            nested = value.get(key)
            if isinstance(nested, dict):
                if clean_entity and isinstance(nested.get(clean_entity), dict):
                    return nested[clean_entity]
                if any(candidate in nested for candidate in ("items", "queue_items", "queue_state", "queue", "current_item", "items_before", "items_after")):
                    return nested
        return value

    @staticmethod
    def _queue_payload_items(value: Any, *, depth: int = 0) -> list[Any]:
        """Return visible queue items from all common provider response shapes."""
        if depth > 5:
            return []
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            return []
        for key in ("items", "queue_items"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
        items: list[Any] = []
        for key in ("previous_items", "items_before"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                items.extend(candidate)
        for key in ("current_item", "active_item", "playing_item"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                items.append(candidate)
        for key in ("next_item",):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                items.append(candidate)
        for key in ("next_items", "items_after"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                items.extend(candidate)
        if items:
            return items
        best: list[Any] = []
        for key in ("response", "data", "queue", "queue_state", "state", "result"):
            candidate = HomeiiFlowRuntime._queue_payload_items(value.get(key), depth=depth + 1)
            if len(candidate) > len(best):
                best = candidate
        for child in value.values():
            candidate = HomeiiFlowRuntime._queue_payload_items(child, depth=depth + 1)
            if len(candidate) > len(best):
                best = candidate
        return best

    def _player_snapshot_for_entity(self, entity_id: str) -> dict[str, Any] | None:
        """Return the Engine player snapshot for an entity id."""
        clean_entity = _clean_string(entity_id)
        if not clean_entity:
            return None
        direct_player = self._ma_players_by_entity.get(clean_entity) or self._ma_players_by_id.get(clean_entity)
        if direct_player:
            return direct_player
        for player in self.media_players_snapshot():
            if player.get("entity_id") == clean_entity:
                return player
        return None

    def _queue_item_from_player(self, player: dict[str, Any] | None) -> dict[str, Any] | None:
        """Build a current queue item from the selected player state as a last Engine-owned source."""
        if not player:
            return None
        title = _clean_string(player.get("media_title") or player.get("attributes", {}).get("media_title"))
        if not title:
            return None
        return {
            "media_title": title,
            "name": title,
            "media_artist": _clean_string(player.get("media_artist") or player.get("attributes", {}).get("media_artist")),
            "media_album_name": _clean_string(player.get("media_album_name") or player.get("attributes", {}).get("media_album_name")),
            "media_content_id": _clean_string(player.get("media_content_id") or player.get("attributes", {}).get("media_content_id")),
            "media_content_type": _clean_string(player.get("media_content_type") or player.get("attributes", {}).get("media_content_type") or "track"),
            "media_duration": player.get("media_duration") or player.get("attributes", {}).get("media_duration"),
            "homeii_artwork_url": player.get("homeii_artwork_url") or player.get("attributes", {}).get("homeii_artwork_url"),
            "image": player.get("homeii_artwork_url") or player.get("entity_picture") or player.get("attributes", {}).get("entity_picture"),
        }

    def normalize_queue_response(
        self,
        response: Any,
        *,
        entity_id: str = "",
        queue_id: str = "",
        player: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a card-ready queue snapshot from a provider response."""
        root = self._queue_payload_root(response, entity_id)
        queue_state = root.get("queue_state") if isinstance(root, dict) and isinstance(root.get("queue_state"), dict) else None
        if queue_state is None and isinstance(root, dict) and isinstance(root.get("queue"), dict):
            queue_state = root.get("queue")
        if queue_state is None and isinstance(root, dict):
            queue_state = root
        if queue_state is None:
            queue_state = {}

        raw_items = self._queue_payload_items(root)
        if not raw_items and isinstance(queue_state, dict):
            raw_items = self._queue_payload_items(queue_state)
        if not raw_items and not isinstance(queue_state.get("items"), list):
            current_from_player = self._queue_item_from_player(player)
            raw_items = [current_from_player] if current_from_player else []

        previous_count = 0
        if isinstance(root, dict):
            previous_items = root.get("previous_items") or root.get("items_before") or queue_state.get("previous_items") or queue_state.get("items_before")
            previous_count = len(previous_items) if isinstance(previous_items, list) else 0
        explicit_index = _maybe_number(
            _dict_first(queue_state, "current_index", "active_index", "index")
            or (root.get("current_index") if isinstance(root, dict) else None)
        )
        current_index = int(explicit_index) if explicit_index is not None else (None if "current_index" in queue_state else previous_count)

        normalized_items: list[dict[str, Any]] = []
        for index, item in enumerate(raw_items):
            normalized = self.normalize_media_item(
                item,
                fallback_media_type=_clean_string(_dict_first(queue_state, "media_type", "media_content_type") or "track"),
                fallback_index=index,
                player=player,
            )
            if normalized:
                normalized["sort_index"] = index
                normalized_items.append(normalized)

        expected = _maybe_number(
            _dict_first(queue_state, "items_count", "items", "total", "total_items", "count", "queue_length")
            or (root.get("items_count") if isinstance(root, dict) else None)
        )
        item_count = int(expected) if expected is not None and expected >= 0 else len(normalized_items)
        def item_at(index: int | None, key: str) -> dict[str, Any] | None:
            if index is not None and 0 <= index < len(normalized_items):
                return normalized_items[index]
            raw = queue_state.get(key)
            return self.normalize_media_item(raw, fallback_index=index or 0) if isinstance(raw, dict) else None

        current_item = item_at(current_index, "current_item")
        previous_item = item_at(current_index - 1 if current_index is not None else None, "previous_item")
        next_item = item_at(current_index + 1 if current_index is not None else None, "next_item")
        root_queue_id = _dict_first(root, "queue_id", "active_queue") if isinstance(root, dict) else ""
        clean_queue_id = _clean_string(
            queue_id
            or _dict_first(queue_state, "queue_id", "active_queue", "queue")
            or root_queue_id
        )
        return {
            "entity_id": _clean_string(entity_id),
            "queue_id": clean_queue_id,
            "active_queue": _clean_string(
                (player or {}).get("active_queue")
                or (player or {}).get("attributes", {}).get("active_queue")
                or clean_queue_id
            ),
            "active_source": _clean_string(
                (player or {}).get("active_source")
                or (player or {}).get("attributes", {}).get("active_source")
            ),
            "queue_active": bool(
                (player or {}).get("queue_active")
                or (player or {}).get("attributes", {}).get("queue_active")
            ),
            "display_name": _clean_string(_dict_first(queue_state, "display_name", "name")),
            "state": _clean_string(_dict_first(queue_state, "state", "playback_state")),
            "available": _dict_first(queue_state, "available", "is_available"),
            "items": normalized_items,
            "items_count": item_count,
            "visible_items": len(normalized_items),
            "current_index": current_index,
            "index_in_buffer": _dict_first(queue_state, "index_in_buffer"),
            "current_item": current_item,
            "previous_item": previous_item,
            "next_item": next_item,
            "shuffle_enabled": bool(
                _dict_first(queue_state, "shuffle_enabled", "shuffle")
            ),
            "repeat_mode": _clean_string(_dict_first(queue_state, "repeat_mode", "repeat")),
            "autoplay_enabled": bool(
                _dict_first(
                    queue_state,
                    "dont_stop_the_music_enabled",
                    "autoplay_enabled",
                    "autoplay",
                )
            ),
            "crossfade_enabled": bool(
                _dict_first(queue_state, "crossfade_enabled", "crossfade")
            ),
            "flow_mode": bool(_dict_first(queue_state, "flow_mode", "flow_mode_enabled")),
            "is_dynamic": bool(_dict_first(queue_state, "is_dynamic", "dynamic")),
            "ended": bool(_dict_first(queue_state, "ended", "finished")),
            "smart_shuffle_active": bool(
                _dict_first(queue_state, "smart_shuffle_active")
            ),
            "smart_fades_active": bool(
                _dict_first(queue_state, "smart_fades_active")
            ),
            "playback_speed": _dict_first(queue_state, "playback_speed"),
            "overlay_enabled": bool(_dict_first(queue_state, "overlay_enabled")),
            "overlay_source": _dict_first(queue_state, "overlay_source"),
            "overlay_volume": _dict_first(queue_state, "overlay_volume"),
            "announcement_in_progress": bool(
                _dict_first(queue_state, "announcement_in_progress", "overlay_active")
            ),
            "radio_source": _dict_first(queue_state, "radio_source"),
            "elapsed_time": _dict_first(queue_state, "elapsed_time", "media_position", "position"),
            "elapsed_time_last_updated": _dict_first(queue_state, "elapsed_time_last_updated", "media_position_updated_at"),
            "raw_keys": list(root.keys())[:24] if isinstance(root, dict) else [],
        }

    @staticmethod
    def _queue_payload_expected_count(value: Any, *, depth: int = 0) -> int:
        """Return the expected full queue length advertised by a provider response."""
        if depth > 5 or not isinstance(value, dict):
            return 0
        for key in ("items", "items_count", "total", "total_items", "count", "queue_length"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                continue
            try:
                number = int(candidate)
            except (TypeError, ValueError):
                continue
            if number >= 0:
                return number
        for key in ("response", "data", "queue", "queue_state", "state", "result"):
            number = HomeiiFlowRuntime._queue_payload_expected_count(value.get(key), depth=depth + 1)
            if number:
                return number
        return 0

    async def async_get_queue(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the richest queue snapshot through the integration-owned backend path."""
        request_revision = self._snapshot_revisions["queue"]
        entity_id = str(payload.get("entity_id") or payload.get("selected_player") or "").strip()
        queue_id = str(payload.get("queue_id") or "").strip()
        include_diagnostics = bool(payload.get("include_diagnostics", False))
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        player = self._player_snapshot_for_entity(entity_id)
        if not queue_id and player:
            queue_id = _clean_string(
                player.get("active_queue")
                or player.get("queue_id")
                or player.get("attributes", {}).get("active_queue")
                or player.get("attributes", {}).get("queue_id")
            )

        def bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(payload.get(key, default))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(maximum, value))

        limit_before = bounded_int("limit_before", 50, 0, 250)
        limit_after = bounded_int("limit_after", 450, 50, 450)
        cache_key = (entity_id, queue_id, limit_before, limit_after)
        singleflight_owner = bool(payload.get("_singleflight_owner"))
        if not singleflight_owner:
            cached = self._queue_cache.get(cache_key)
            if (
                cached
                and int(cached.get("revision") or 0) == request_revision
                and time.monotonic() - float(cached.get("stored_at") or 0) <= 2.0
            ):
                response = copy.deepcopy(cached["result"])
                response["cache"] = {"hit": True, "source": "engine_queue_memory", "ttl": 2}
                return response
            existing_task = self._queue_inflight.get(cache_key)
            if existing_task is not None:
                return copy.deepcopy(await asyncio.shield(existing_task))
            foreground_task = self.hass.async_create_task(
                self.async_get_queue({**payload, "_singleflight_owner": True})
            )
            self._queue_inflight[cache_key] = foreground_task
            try:
                return copy.deepcopy(await asyncio.shield(foreground_task))
            finally:
                if self._queue_inflight.get(cache_key) is foreground_task and foreground_task.done():
                    self._queue_inflight.pop(cache_key, None)
        attempts: list[dict[str, Any]] = []
        bridge_results = await self._try_music_queue_command_bridge(
            entity_id=entity_id,
            queue_id=queue_id,
            player=player,
            limit_before=limit_before,
            limit_after=limit_after,
            profile_id=profile_id,
        )
        direct = next(
            (item for item in reversed(bridge_results) if isinstance(item.get("normalized"), dict)),
            None,
        )
        if direct is None:
            detail = next(
                (
                    str(item.get("error") or "")
                    for item in reversed(bridge_results)
                    if item.get("error")
                ),
                "Music Assistant could not resolve an active queue for this player.",
            )
            raise HomeiiFlowServiceUnavailable(detail)
        normalized = direct["normalized"]
        result = {
            "provider": direct["provider"],
            "source_of_truth": "homeii_flow_engine",
            "normalized": normalized,
            "items": normalized.get("items", []),
            "coverage": {
                "visible_items": direct.get("visible_items", 0),
                "expected_items": direct.get("expected_items", 0),
                "proxy_artwork_items": direct.get("proxy_artwork_items", 0),
                "complete": direct.get("complete", False),
            },
        }
        if include_diagnostics:
            result["data"] = direct.get("response")
            result["responses"] = [
                {key: value for key, value in item.items() if key not in {"response", "normalized", "score"}}
                for item in bridge_results
            ]
        return self._cache_queue_result(
            cache_key,
            self._queue_result_with_snapshot(
                result,
                entity_id=entity_id,
                queue_id=_clean_string(normalized.get("queue_id")) or queue_id,
                revision=request_revision,
            ),
        )

    def _library_cache_entry(
        self,
        cache_key: tuple[Any, ...],
    ) -> tuple[tuple[Any, ...], dict[str, Any] | None]:
        """Return an exact cache entry or the smallest compatible larger shelf."""
        exact = self._library_cache.get(cache_key)
        if exact is not None:
            return cache_key, exact
        requested_limit = int(cache_key[2])
        compatible = [
            (key, value)
            for key, value in self._library_cache.items()
            if len(key) == len(cache_key)
            and key[:2] == cache_key[:2]
            and key[3:] == cache_key[3:]
            and int(key[2]) >= requested_limit
        ]
        if not compatible:
            return cache_key, None
        return min(compatible, key=lambda item: int(item[0][2]))

    def _library_response(
        self,
        result: dict[str, Any],
        *,
        limit: int,
        compact: bool,
    ) -> dict[str, Any]:
        """Bound library output and omit duplicated provider payload for card reads."""
        response = dict(result)
        if not isinstance(response.get("snapshot"), dict):
            response["snapshot"] = self._snapshot_meta(
                "library",
                identity=_clean_string(response.get("media_type")),
            )
        items = response.get("items")
        if isinstance(items, list):
            response["items"] = items[:limit]
        if compact:
            response.pop("data", None)
            response.pop("attempts", None)
            response["compact"] = True
        return response

    async def async_get_library(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return library data when the Music Assistant HA service is available."""
        media_type = str(payload.get("media_type") or payload.get("type") or "playlist").strip()
        limit = int(payload.get("limit") or 50)
        clean_limit = max(1, min(limit, 500))
        offset = max(0, int(payload.get("offset") or 0))
        data = {"media_type": media_type, "limit": clean_limit}
        order_by = str(payload.get("order_by") or "").strip()
        if order_by:
            data["order_by"] = order_by
        favorite_only = bool(payload.get("favorite", False) or payload.get("favorites_only", False))
        if favorite_only:
            data["favorite"] = True
        query = str(
            payload.get("query")
            or payload.get("search")
            or payload.get("search_query")
            or payload.get("name")
            or ""
        ).strip()
        if query:
            data["search"] = query
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        cache_key = (
            media_type,
            data.get("order_by", ""),
            data["limit"],
            favorite_only,
            query.lower(),
            offset,
        )
        refresh_requested = bool(payload.get("_refresh"))
        singleflight_owner = bool(payload.get("_singleflight_owner"))
        compact_requested = bool(payload.get("compact"))
        now_mono = time.monotonic()
        resolved_cache_key, cached = self._library_cache_entry(cache_key)
        if cached and not refresh_requested:
            cached_result = cached.get("result")
            if isinstance(cached_result, dict) and float(cached.get("fresh_until") or 0) > now_mono:
                self._media_cache_metrics["memory_hits"] += 1
                if cached.get("persistent"):
                    self._media_cache_metrics["persistent_hits"] += 1
                    cached["persistent"] = False
                return self._library_response(cached_result | {
                    "cache": {
                        "hit": True,
                        "stale": False,
                        "source": "engine_memory" if resolved_cache_key == cache_key else "engine_memory_superset",
                        "ttl": max(0, int(float(cached.get("fresh_until") or 0) - now_mono)),
                    }
                }, limit=clean_limit, compact=compact_requested)
            if isinstance(cached_result, dict) and float(cached.get("stale_until") or 0) > now_mono:
                self._media_cache_metrics["stale_hits"] += 1
                if resolved_cache_key not in self._library_inflight:
                    self._media_cache_metrics["background_refreshes"] += 1
                    refresh_task = self.hass.async_create_task(
                        self.async_get_library(
                            {
                                **payload,
                                "_refresh": True,
                                "_singleflight_owner": True,
                            }
                        )
                    )
                    self._library_inflight[resolved_cache_key] = refresh_task

                    def finish_refresh(task: asyncio.Task[dict[str, Any]]) -> None:
                        if self._library_inflight.get(resolved_cache_key) is task:
                            self._library_inflight.pop(resolved_cache_key, None)
                        try:
                            task.result()
                        except Exception:  # noqa: BLE001 - stale data remains available
                            self._media_cache_metrics["refresh_failures"] += 1

                    refresh_task.add_done_callback(finish_refresh)
                return self._library_response(cached_result | {
                    "cache": {
                        "hit": True,
                        "stale": True,
                        "source": "engine_stale_while_revalidate" if resolved_cache_key == cache_key else "engine_stale_superset",
                        "ttl": 0,
                    }
                }, limit=clean_limit, compact=compact_requested)
        if not singleflight_owner:
            existing_task = self._library_inflight.get(cache_key)
            if existing_task is not None:
                self._media_cache_metrics["coalesced"] += 1
                return await asyncio.shield(existing_task)
            self._media_cache_metrics["misses"] += 1
            foreground_task = self.hass.async_create_task(
                self.async_get_library(
                    {
                        **payload,
                        "_refresh": refresh_requested,
                        "_singleflight_owner": True,
                    }
                )
            )
            self._library_inflight[cache_key] = foreground_task
            try:
                return await asyncio.shield(foreground_task)
            finally:
                if self._library_inflight.get(cache_key) is foreground_task and foreground_task.done():
                    self._library_inflight.pop(cache_key, None)
        fetch_started = time.perf_counter()
        fetch_revision = self._snapshot_revisions["library"]
        bridge_response, bridge_items, bridge_attempts = await self._try_music_library_command_bridge(
            media_type=media_type,
            limit=clean_limit,
            offset=offset,
            order_by=order_by,
            query=query,
            favorite_only=favorite_only,
            profile_id=profile_id,
        )
        if bridge_response is None:
            detail = next(
                (
                    str(attempt.get("error") or "")
                    for attempt in reversed(bridge_attempts)
                    if attempt.get("error")
                ),
                "Music Assistant library API returned no response.",
            )
            raise HomeiiFlowServiceUnavailable(detail)
        result = {
            "provider": "music_assistant.server_command",
            "source_of_truth": "homeii_flow_engine",
            "media_type": media_type,
            "offset": offset,
            "snapshot": self._snapshot_meta("library", identity=media_type, revision=fetch_revision),
            "data": bridge_response,
            "items": bridge_items,
            "attempts": bridge_attempts,
            "cache": {"hit": False, "stale": False, "source": "music_assistant_2_10_api", "ttl": 600 if bridge_items else 30},
        }
        fresh_ttl = 600 if bridge_items else 30
        stale_ttl = 24 * 60 * 60 if bridge_items else 5 * 60
        self._library_cache[cache_key] = {
            "fresh_until": time.monotonic() + fresh_ttl,
            "stale_until": time.monotonic() + stale_ttl,
            "stored_at": time.time(),
            "result": result,
            "persistent": False,
            "invalidated": False,
        }
        self._media_cache_metrics["last_fetch_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
        self._media_cache_metrics["last_fetch_at"] = _utc_iso()
        self._schedule_media_cache_save()
        return self._library_response(result, limit=clean_limit, compact=compact_requested)

    async def async_get_favorites(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return one aggregated, artwork-decorated favorites snapshot."""
        request_revision = self._snapshot_revisions["library"]
        requested_types = _safe_list(
            payload.get("media_types")
            or ["track", "album", "playlist", "radio", "podcast", "artist"]
        )
        media_types = [
            _clean_string(media_type).lower()
            for media_type in requested_types
            if _clean_string(media_type).lower()
            in {"track", "album", "playlist", "radio", "podcast", "artist", "audiobook"}
        ]
        if not media_types:
            media_types = ["track", "album", "playlist", "radio", "podcast", "artist"]
        limit = _bounded_int(payload.get("limit"), 160, 1, 500)
        refresh = bool(payload.get("refresh", False))
        results = await asyncio.gather(
            *(
                self.async_get_library(
                    {
                        "media_type": media_type,
                        "limit": limit if media_type == "track" else min(limit, 100),
                        "favorite": True,
                        "compact": True,
                        "_refresh": refresh,
                    }
                )
                for media_type in media_types
            ),
            return_exceptions=True,
        )
        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        seen: set[str] = set()
        for media_type, result in zip(media_types, results, strict=False):
            if isinstance(result, BaseException):
                errors.append({"media_type": media_type, "error": str(result)})
                continue
            for item in result.get("items", []):
                if not isinstance(item, dict):
                    continue
                uri = _clean_string(item.get("uri") or item.get("media_content_id"))
                key = uri or f"{media_type}:{_clean_string(item.get('name') or item.get('title')).lower()}"
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append({**item, "media_type": item.get("media_type") or media_type, "favorite": True})
        return {
            "provider": "homeii_flow_engine.favorites",
            "source_of_truth": "homeii_flow_engine",
            "items": items,
            "media_types": media_types,
            "errors": errors,
            "snapshot": self._snapshot_meta(
                "library",
                identity="favorites",
                revision=request_revision,
            ),
            "generated_at": _utc_iso(),
        }

    async def async_set_favorite(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or remove a Music Assistant favorite through one Engine contract."""
        favorite = bool(payload.get("favorite", True))
        entry = payload.get("entry") if isinstance(payload.get("entry"), dict) else {}
        remove_args = payload.get("remove_args") if isinstance(payload.get("remove_args"), dict) else {}
        uri = _clean_string(payload.get("uri") or entry.get("uri") or entry.get("media_content_id"))
        media_type = _clean_string(
            payload.get("media_type")
            or entry.get("media_type")
            or entry.get("type")
            or "track"
        ).lower()
        item_id = _clean_string(payload.get("item_id") or entry.get("item_id") or entry.get("id"))
        provider = _clean_string(
            payload.get("provider")
            or entry.get("provider")
            or entry.get("provider_instance")
            or entry.get("provider_domain")
        )
        library_item_id = _clean_string(
            payload.get("library_item_id")
            or remove_args.get("library_item_id")
            or entry.get("library_item_id")
        )
        if favorite and not (uri or item_id):
            raise ValueError("uri or item_id is required to add a favorite")
        if not favorite and not library_item_id:
            raise ValueError("library_item_id is required to remove a favorite")

        if favorite:
            best_item = uri or item_id
            attempts = [
                {"item": best_item},
                {"item": item_id or best_item},
                {
                    "item": {
                        "uri": uri,
                        "media_type": media_type,
                        "item_id": item_id,
                        "provider": provider,
                    }
                },
            ]
            command = "music/favorites/add_item"
        else:
            attempts = [
                {
                    "media_type": _clean_string(remove_args.get("media_type") or media_type),
                    "library_item_id": library_item_id,
                },
                {"library_item_id": library_item_id},
            ]
            command = "music/favorites/remove_item"

        last_error = ""
        response: dict[str, Any] | None = None
        used_args: dict[str, Any] = {}
        for attempt in attempts:
            clean_args = {
                key: value
                for key, value in attempt.items()
                if value not in (None, "", {})
            }
            if isinstance(clean_args.get("item"), dict):
                clean_args["item"] = {
                    key: value
                    for key, value in clean_args["item"].items()
                    if value not in (None, "")
                }
            try:
                response = await self.async_music_assistant_command(
                    {"command": command, "args": clean_args}
                )
                used_args = clean_args
                break
            except Exception as err:  # noqa: BLE001 - MA schemas vary by beta
                last_error = str(err)
        if response is None:
            raise HomeiiFlowServiceUnavailable(last_error or f"{command} failed")

        self._mark_library_cache_stale({media_type} if media_type else None)
        self._mark_media_command_cache_stale()
        self._search_cache.clear()
        self._bump_snapshot_revision("library", reason=f"favorite:{'add' if favorite else 'remove'}")
        snapshot = self._snapshot_meta("library", identity="favorites")
        event = {
            "kind": "event",
            "event": "favorite_changed",
            "favorite": favorite,
            "media_type": media_type,
            "uri": uri,
            "snapshot": snapshot,
        }
        self.hass.bus.async_fire(EVENT_MUSIC_ASSISTANT, event)
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        return {
            "ok": True,
            "favorite": favorite,
            "media_type": media_type,
            "uri": uri,
            "library_item_id": library_item_id,
            "provider": f"music_assistant.server_command:{command}",
            "used_args": used_args,
            "snapshot": snapshot,
            "executed_at": _utc_iso(),
        }

    def normalize_search_response(self, response: Any, *, media_types: list[str] | None = None) -> dict[str, Any]:
        """Return normalized grouped and flat search results."""
        decorated = self.decorate_artwork_urls(response)
        groups: dict[str, list[dict[str, Any]]] = {}
        flat_items: list[dict[str, Any]] = []
        wanted = {str(media_type or "").strip().lower() for media_type in (media_types or []) if str(media_type or "").strip()}
        group_aliases = {
            "items": "",
            "media": "",
            "media_items": "",
            "library_items": "",
            "results": "",
            "playlists": "playlist",
            "playlist": "playlist",
            "artists": "artist",
            "artist": "artist",
            "albums": "album",
            "album": "album",
            "tracks": "track",
            "track": "track",
            "radio": "radio",
            "radios": "radio",
            "podcasts": "podcast",
            "podcast": "podcast",
            "genres": "genre",
            "genre": "genre",
        }
        output_groups = {
            "album": "albums",
            "artist": "artists",
            "genre": "genres",
            "playlist": "playlists",
            "podcast": "podcasts",
            "radio": "radio",
            "track": "tracks",
        }

        def add_group(group_key: str, value: Any) -> None:
            fallback_type = group_aliases.get(group_key.lower(), group_key.lower() or "track")
            if wanted and fallback_type and fallback_type not in wanted and group_key.lower() not in wanted:
                return
            items: list[dict[str, Any]] = []
            for index, raw_item in enumerate(_extract_media_items(value)):
                item_type = _media_item_type(raw_item, fallback_type or "track").lower()
                if wanted and item_type not in wanted and fallback_type not in wanted and group_key.lower() not in wanted:
                    continue
                normalized_item = self.normalize_media_item(raw_item, fallback_media_type=item_type or fallback_type or "track", fallback_index=index)
                if normalized_item:
                    items.append(normalized_item)
            if items:
                output_key = output_groups.get(fallback_type) or output_groups.get(items[0].get("media_type")) or group_key
                groups.setdefault(output_key, []).extend(items)
                flat_items.extend(items)

        if isinstance(decorated, dict) and _looks_like_media_item(decorated):
            add_group("items", [decorated])
        elif isinstance(decorated, dict):
            for key, value in decorated.items():
                if key in {"response", "data", "result", "results"} and isinstance(value, dict):
                    for nested_key, nested_value in value.items():
                        add_group(str(nested_key), nested_value)
                else:
                    add_group(str(key), value)
        elif isinstance(decorated, list):
            add_group("items", decorated)
        return {
            "data": decorated,
            "groups": groups,
            "items": flat_items,
        }

    async def async_get_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return Music Assistant search results through the Engine-owned backend path."""
        query = str(
            payload.get("query")
            or payload.get("search")
            or payload.get("search_query")
            or payload.get("name")
            or ""
        ).strip()
        limit = max(1, min(int(payload.get("limit") or 25), 80))
        media_types = _safe_list(
            payload.get("media_types")
            or payload.get("media_type")
            or ["radio", "podcast", "album", "artist", "track", "playlist", "genre"]
        )
        media_types = [str(media_type).strip() for media_type in media_types if str(media_type).strip()]
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        library_only = bool(payload.get("library_only", False))
        provider_only = bool(payload.get("provider_only", False))
        provider_filter = tuple(
            sorted(
                _clean_string(provider)
                for provider in _safe_list(payload.get("providers") or payload.get("provider"))
                if _clean_string(provider)
            )
        )
        cache_key = (
            query.lower(),
            limit,
            tuple(media_types),
            library_only,
            provider_only,
            provider_filter,
        )
        cached = self._search_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        attempts: list[dict[str, Any]] = []
        if not query:
            return {
                "provider": "music_assistant.search",
                "source_of_truth": "homeii_flow_engine",
                "data": {},
                "groups": {},
                "items": [],
                "attempts": attempts,
            }
        requested_providers = [
            _clean_string(provider)
            for provider in _safe_list(payload.get("providers") or payload.get("provider"))
            if _clean_string(provider)
        ]
        if library_only:
            requested_providers = ["library"]
        elif provider_only and not requested_providers:
            requested_providers = await self._music_assistant_provider_ids()
        args: dict[str, Any] = {
            "search_query": query,
            "media_types": media_types,
            "limit": limit,
        }
        if requested_providers:
            args["providers"] = requested_providers
        response = await self.async_music_assistant_command(
            {"command": "music/search", "args": args, "profile_id": profile_id}
        )
        data = response.get("data") if isinstance(response, dict) else response
        normalized = self.normalize_search_response(data, media_types=media_types)
        attempts.append(
            {
                "provider": "music_assistant.server_command",
                "command": "music/search",
                "providers": requested_providers or ["library", "all_available_providers"],
                "ok": True,
                "items": len(normalized["items"]),
            }
        )
        result = {
            "provider": "music_assistant.server_command",
            "source_of_truth": "homeii_flow_engine",
            "data": normalized["data"],
            "groups": normalized["groups"],
            "items": normalized["items"],
            "providers": requested_providers,
            "attempts": attempts,
            "cache": {"hit": False, "ttl": 30},
        }
        self._search_cache[cache_key] = (
            time.monotonic() + 30,
            result | {"cache": {"hit": True, "ttl": 30}},
        )
        return result

    async def async_apply_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply group changes using native MA identities, including MA-only players."""
        owner = _clean_string(payload.get("owner") or payload.get("entity_id"))
        members = list(dict.fromkeys(_clean_string(value) for value in _safe_list(payload.get("members")) if _clean_string(value)))
        removed = list(dict.fromkeys(_clean_string(value) for value in _safe_list(payload.get("remove_members")) if _clean_string(value)))
        clear_all = bool(payload.get("clear_all"))
        if not owner:
            raise ValueError("group owner is required")
        if clear_all:
            for entity_id in dict.fromkeys([*members, *removed, owner]):
                await self.async_player_command({"player": entity_id, "command": "unjoin"})
        else:
            await self.async_music_assistant_command({
                "command": "players/cmd/set_members",
                "args": {
                    "target_player": self._resolve_ma_player_id(owner),
                    "player_ids_to_add": [self._resolve_ma_player_id(value) for value in members if value != owner],
                    "player_ids_to_remove": [self._resolve_ma_player_id(value) for value in removed if value != owner],
                },
            })
        self._stats_cache = None
        self._bump_snapshot_revision("players", "queue", reason="group_apply")
        result = {"mode": "clear_all" if clear_all else "apply", "owner": owner, "members": members, "remove_members": removed}
        self.hass.bus.async_fire(EVENT_ENGINE_GROUP_APPLY, result)
        async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        return result

    def schedules(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        """Return schedules, optionally filtered by profile."""
        clean_profile = str(profile_id or "").strip()
        schedules = _safe_list(self._storage.get("schedules"))
        if not clean_profile:
            return schedules
        return [item for item in schedules if str(item.get("profile_id") or DEFAULT_PROFILE_ID) == clean_profile]

    def timers(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        """Return timers, optionally filtered by profile."""
        clean_profile = str(profile_id or "").strip()
        timers = _safe_list(self._storage.get("timers"))
        if not clean_profile:
            return timers
        return [item for item in timers if str(item.get("profile_id") or DEFAULT_PROFILE_ID) == clean_profile]

    def volume_rules(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        """Return volume rules, optionally filtered by profile."""
        clean_profile = str(profile_id or "").strip()
        rules = _safe_list(self._storage.get("volume_rules"))
        if not clean_profile:
            return rules
        return [item for item in rules if str(item.get("profile_id") or DEFAULT_PROFILE_ID) == clean_profile]

    def announcements(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        """Return recorded announcements, optionally filtered by profile."""
        clean_profile = str(profile_id or "").strip()
        announcements = _safe_list(self._storage.get("announcements"))
        if not clean_profile:
            return announcements
        return [item for item in announcements if str(item.get("profile_id") or DEFAULT_PROFILE_ID) == clean_profile]

    def activity(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        """Return recent Engine activity, optionally filtered by profile."""
        clean_profile = str(profile_id or "").strip()
        activity = _safe_list(self._storage.get("activity"))
        if not clean_profile:
            return activity
        return [item for item in activity if str(item.get("profile_id") or DEFAULT_PROFILE_ID) == clean_profile]

    def last_activity(self, profile_id: str | None = None) -> dict[str, Any]:
        """Return the latest Engine activity item."""
        items = self.activity(profile_id)
        return items[0] if items else {}

    def activity_count(self, profile_id: str | None = None) -> int:
        """Return recent activity count."""
        return len(self.activity(profile_id))

    async def async_record_activity(
        self,
        kind: str,
        message: str,
        *,
        profile_id: str = DEFAULT_PROFILE_ID,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a compact Engine activity item."""
        item = {
            "kind": str(kind or "event").strip() or "event",
            "message": str(message or "").strip(),
            "profile_id": str(profile_id or DEFAULT_PROFILE_ID),
            "data": data or {},
            "created_at": _utc_iso(),
        }
        self._storage["activity"] = [item, *self.activity()][:50]
        await self.async_save()
        return item

    async def async_set_volume_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store or update a volume rule."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
        player = str(payload.get("player") or payload.get("entity_id") or "").strip()
        if not player:
            raise ValueError("player is required")
        rule = {
            "profile_id": profile_id,
            "player": player,
            "enabled": bool(payload.get("enabled", True)),
            "max_volume": max(0, min(100, int(payload.get("max_volume") or 0))),
            "start_time": str(payload.get("start_time") or ""),
            "end_time": str(payload.get("end_time") or ""),
            "days": [int(day) for day in _safe_list(payload.get("days")) if str(day).strip()],
            "updated_at": _utc_iso(),
        }
        rules = [
            existing
            for existing in self.volume_rules()
            if not (existing.get("profile_id") == profile_id and existing.get("player") == player)
        ]
        rules.append(rule)
        self._storage["volume_rules"] = rules
        self._stats_cache = None
        await self.async_save()
        results = await self.async_enforce_volume_rules()
        if results:
            async_dispatcher_send(self.hass, SIGNAL_ENGINE_UPDATED)
        await self.async_record_activity(
            "volume_rule_saved",
            f"Volume rule saved for {player}",
            profile_id=profile_id,
            data={"player": player, "max_volume": rule.get("max_volume"), "active_now": self._volume_rule_active(rule, _local_datetime())},
        )
        return rule

    async def async_clear_volume_rules(self, profile_id: str) -> dict[str, Any]:
        """Clear volume rules for a profile."""
        before = len(self.volume_rules())
        self._storage["volume_rules"] = [
            rule for rule in self.volume_rules() if str(rule.get("profile_id") or DEFAULT_PROFILE_ID) != profile_id
        ]
        removed = before - len(self.volume_rules())
        self._stats_cache = None
        await self.async_save()
        await self.async_record_activity(
            "volume_rules_cleared",
            f"Volume rules cleared: {removed}",
            profile_id=profile_id,
            data={"removed": removed},
        )
        return {"profile_id": profile_id, "removed": removed}

    async def async_delete_volume_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete one volume rule for a profile/player."""
        profile_id = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
        player = str(payload.get("player") or payload.get("entity_id") or "").strip()
        if not player:
            raise ValueError("player is required")
        before = len(self.volume_rules())
        self._storage["volume_rules"] = [
            rule
            for rule in self.volume_rules()
            if not (str(rule.get("profile_id") or DEFAULT_PROFILE_ID) == profile_id and rule.get("player") == player)
        ]
        removed = before - len(self.volume_rules())
        self._stats_cache = None
        await self.async_save()
        await self.async_record_activity(
            "volume_rule_deleted",
            f"Volume rule deleted for {player}",
            profile_id=profile_id,
            data={"player": player, "removed": removed},
        )
        return {"profile_id": profile_id, "player": player, "removed": removed}

    async def async_enforce_volume_rules(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Apply active volume rules to current media player state."""
        local_now = _local_datetime(now)
        results: list[dict[str, Any]] = []
        for rule in self.volume_rules():
            if not self._volume_rule_active(rule, local_now):
                continue
            player = str(rule.get("player") or "").strip()
            if not player:
                continue
            state = self.hass.states.get(player)
            if state is None:
                continue
            current_level = state.attributes.get("volume_level")
            if not isinstance(current_level, (int, float)):
                continue
            max_level = max(0, min(100, int(rule.get("max_volume") or 0))) / 100
            if current_level <= max_level + 0.005:
                continue
            await self.async_call_service_response(
                "media_player",
                "volume_set",
                {"entity_id": player, "volume_level": max_level},
                return_response=False,
            )
            result = {
                "player": player,
                "previous_level": round(float(current_level), 3),
                "new_level": round(max_level, 3),
                "rule": rule,
                "applied_at": _utc_iso(),
            }
            self._last_volume_action = result
            results.append(result)
            await self.async_record_activity(
                "volume_rule_applied",
                f"Volume limited for {player}",
                profile_id=str(rule.get("profile_id") or DEFAULT_PROFILE_ID),
                data=result,
            )
        return results

    def _volume_rule_active(self, rule: dict[str, Any], now: datetime) -> bool:
        """Return whether a volume rule applies right now."""
        if not bool(rule.get("enabled", True)):
            return False
        days = [int(day) for day in _safe_list(rule.get("days")) if str(day).strip()]
        if days and _homeii_weekday(now) not in days:
            return False
        return _time_window_active(now, str(rule.get("start_time") or ""), str(rule.get("end_time") or ""))

    def _preferred_announcement_say_service(self, message: str = "") -> str:
        """Return a usable legacy tts *_say service when tts.speak is not configured."""
        services = self.hass.services.async_services().get("tts", {})
        names = [str(name) for name in services.keys()]
        has_hebrew = any("\u0590" <= char <= "\u05FF" for char in str(message or ""))
        if has_hebrew and "google_translate_say" in names:
            return "google_translate_say"
        if "google_translate_say" in names:
            return "google_translate_say"
        return next((name for name in names if name.endswith("_say")), "")

    def _announcement_targets(self, payload: dict[str, Any]) -> list[str]:
        """Normalize announcement target players."""
        players = [str(player).strip() for player in _safe_list(payload.get("players")) if str(player).strip()]
        single_player = str(payload.get("player") or payload.get("entity_id") or "").strip()
        if single_player and single_player not in players:
            players.insert(0, single_player)
        return players

    async def async_send_announcement(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send an announcement through Home Assistant/Music Assistant and record it."""
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")
        players = self._announcement_targets(payload)
        if not players:
            raise ValueError("at least one target player is required")

        volume = payload.get("volume")
        try:
            announce_volume = max(0, min(100, int(volume))) if volume is not None else None
        except (TypeError, ValueError):
            announce_volume = None
        language = str(payload.get("language") or "").strip()
        tts_entity = str(payload.get("tts_entity") or payload.get("announcement_tts_entity") or "").strip()
        is_url = message.lower().startswith(("http://", "https://"))
        results: list[dict[str, Any]] = []

        for player in players:
            if is_url:
                service_data: dict[str, Any] = {"entity_id": player, "url": message}
                if announce_volume is not None:
                    service_data["announce_volume"] = announce_volume
                try:
                    await self.async_call_service_response(
                        "music_assistant",
                        "play_announcement",
                        service_data,
                        target={"entity_id": player},
                        return_response=False,
                    )
                    results.append({"player": player, "ok": True, "provider": "music_assistant.play_announcement"})
                except Exception as err:  # noqa: BLE001 - surfaced in command result
                    results.append({"player": player, "ok": False, "provider": "music_assistant.play_announcement", "error": str(err)})
                continue

            sent = False
            last_error = ""
            if tts_entity and self.hass.services.has_service("tts", "speak"):
                try:
                    media_id = tts.generate_media_source_id(
                        self.hass, message, engine=tts_entity,
                        language=language or None, cache=False,
                    )
                    service_data = {
                        "media_content_id": media_id,
                        "media_content_type": "music",
                        "announce": True,
                        "extra": {"announce_volume": announce_volume} if announce_volume is not None else {},
                    }
                    await self.async_call_service_response(
                        "media_player", "play_media", service_data,
                        target={"entity_id": player}, return_response=False,
                    )
                    results.append({"player": player, "ok": True, "provider": "media_player.play_media", "tts_entity": tts_entity})
                    sent = True
                except Exception as err:  # noqa: BLE001 - never replay uncertain audio dispatch
                    results.append({"player": player, "ok": False, "provider": "media_player.play_media", "error": str(err)})
                    continue

            if sent:
                continue

            say_service = self._preferred_announcement_say_service(message)
            if say_service:
                service_data = {"entity_id": player, "message": message, "cache": False}
                if language:
                    service_data["language"] = language
                try:
                    await self.async_call_service_response(
                        "tts",
                        say_service,
                        service_data,
                        target={"entity_id": player},
                        return_response=False,
                    )
                    results.append({"player": player, "ok": True, "provider": f"tts.{say_service}"})
                    sent = True
                except Exception as err:  # noqa: BLE001 - collected below
                    last_error = str(err)

            if not sent:
                results.append({
                    "player": player,
                    "ok": False,
                    "provider": "tts",
                    "error": last_error or "No compatible TTS service/entity is available.",
                })

        sent_any = any(bool(result.get("ok")) for result in results)
        sent_all = bool(results) and all(bool(result.get("ok")) for result in results)
        announcement = await self.async_record_announcement(
            {
                **payload,
                "players": players,
                "player": players[0] if len(players) == 1 else str(payload.get("player") or ""),
                "sent": sent_any,
            }
        )
        result = {
            "ok": sent_all,
            "partial": sent_any and not sent_all,
            "sent": sent_any,
            "players": players,
            "results": results,
            "announcement": announcement.get("announcement"),
            "source_of_truth": "homeii_flow_engine",
            "provider": "homeii_flow.announcement_dispatch",
        }
        await self.async_record_activity(
            "announcement_sent" if sent_any else "announcement_failed",
            f"Announcement {'sent' if sent_any else 'failed'} for {len(players)} player(s)",
            profile_id=str(payload.get("profile_id") or DEFAULT_PROFILE_ID),
            data={"players": players, "results": results, "sent": sent_any},
        )
        if not sent_any:
            errors = "; ".join(str(item.get("error") or "") for item in results if item.get("error"))
            raise HomeiiFlowServiceUnavailable(errors or "Announcement could not be sent")
        return result

    async def async_record_announcement(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store an Engine-dispatched announcement in history."""
        players = self._announcement_targets(payload)
        single_player = str(payload.get("player") or payload.get("entity_id") or "").strip()
        announcement = {
            "message": str(payload.get("message") or "").strip(),
            "player": single_player,
            "players": players,
            "profile_id": str(payload.get("profile_id") or DEFAULT_PROFILE_ID),
            "volume": payload.get("volume"),
            "language": str(payload.get("language") or "").strip(),
            "target": str(payload.get("target") or "").strip(),
            "sent": bool(payload.get("sent", True)),
            "created_at": _utc_iso(),
        }
        self._storage["announcements"] = [announcement, *self._storage.get("announcements", [])][:25]
        self._last_announcement_action = announcement
        await self.async_save()
        self.hass.bus.async_fire(EVENT_ENGINE_ANNOUNCEMENT, announcement)
        await self.async_record_activity(
            "announcement_recorded",
            f"Announcement recorded for {single_player or len(players) or 'default target'}",
            profile_id=str(announcement.get("profile_id") or DEFAULT_PROFILE_ID),
            data={
                "player": single_player,
                "players": players,
                "target": announcement.get("target"),
                "sent": announcement.get("sent"),
            },
        )
        return {
            "accepted": True,
            "announcement": announcement,
            "note": "Announcement was recorded by HOMEii Flow Engine.",
        }

    def sendspin_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return Sendspin backend readiness context."""
        return {
            "available": bool(self.music_assistant_base_urls() and self.music_assistant_tokens()),
            "backend_tracks_runtime": False,
            "player_id": str(payload.get("player_id") or ""),
            "note": "Authenticated HA bridge is configured when available; browser connection and audio state are reported by the card.",
        }
