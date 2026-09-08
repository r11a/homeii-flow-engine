"""Calendar entities for HOMEii Flow Engine schedules."""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import async_get_runtime
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, DEFAULT_INSTANCE_ID, DEFAULT_PROFILE_ID, DOMAIN, NAME, SIGNAL_ENGINE_UPDATED, VERSION
from .runtime import HomeiiFlowRuntime, _homeii_weekday, _parse_hhmm, _schedule_days


def _profile_id(entry: ConfigEntry) -> str:
    """Return the active profile id for a config entry."""
    return str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)


def _as_local_datetime(value: date | datetime, *, end_of_day: bool = False) -> datetime:
    """Convert a date or datetime into a local timezone-aware datetime."""
    if isinstance(value, datetime):
        return dt_util.as_local(value if value.tzinfo is not None else value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
    local_time = dt_time.max if end_of_day else dt_time.min
    return datetime.combine(value, local_time, tzinfo=dt_util.DEFAULT_TIME_ZONE)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HOMEii Flow Engine schedule calendar."""
    runtime = async_get_runtime(hass)
    async_add_entities([HomeiiFlowScheduleCalendar(runtime, entry)])


class HomeiiFlowScheduleCalendar(CalendarEntity):
    """Expose HOMEii schedules as a Home Assistant calendar."""

    _attr_has_entity_name = True
    _attr_name = "Schedules calendar"
    _attr_icon = "mdi:calendar-music"

    def __init__(self, runtime: HomeiiFlowRuntime, entry: ConfigEntry) -> None:
        """Initialize the schedule calendar."""
        self._runtime = runtime
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_schedules_calendar"

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
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming schedule event."""
        now = dt_util.now()
        events = self._events_between(now, now + timedelta(days=8), limit=1)
        return events[0] if events else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: date | datetime,
        end_date: date | datetime,
    ) -> list[CalendarEvent]:
        """Return schedule events in the requested range."""
        return self._events_between(start_date, end_date)

    def _events_between(
        self,
        start_date: date | datetime,
        end_date: date | datetime,
        *,
        limit: int = 250,
    ) -> list[CalendarEvent]:
        """Build calendar events from stored HOMEii schedules."""
        profile_id = _profile_id(self._entry)
        start = _as_local_datetime(start_date)
        end = _as_local_datetime(end_date, end_of_day=not isinstance(end_date, datetime))
        if end < start:
            return []

        events: list[CalendarEvent] = []
        cursor = start.date()
        end_day = end.date()
        for schedule in self._runtime.schedules(profile_id):
            events.extend(self._schedule_events(schedule, cursor, end_day, start, end, limit - len(events)))
            if len(events) >= limit:
                break
        events.sort(key=lambda event: event.start)
        return events[:limit]

    def _schedule_events(
        self,
        schedule: dict[str, Any],
        start_day: date,
        end_day: date,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[CalendarEvent]:
        """Return calendar events for a single schedule."""
        if limit <= 0 or not bool(schedule.get("enabled", True)):
            return []
        parsed_time = _parse_hhmm(schedule.get("time"))
        if parsed_time is None:
            return []
        days = _schedule_days(schedule)
        events: list[CalendarEvent] = []
        day = start_day
        while day <= end_day and len(events) < limit:
            event_start = datetime.combine(day, dt_time(parsed_time[0], parsed_time[1]), tzinfo=dt_util.DEFAULT_TIME_ZONE)
            if (not days or _homeii_weekday(event_start) in days) and start <= event_start <= end:
                events.append(self._calendar_event(schedule, event_start))
            day += timedelta(days=1)
        return events

    def _calendar_event(self, schedule: dict[str, Any], event_start: datetime) -> CalendarEvent:
        """Build one Home Assistant calendar event."""
        name = str(schedule.get("name") or schedule.get("media_name") or schedule.get("playlist_name") or "HOMEii schedule")
        media_name = str(schedule.get("media_name") or schedule.get("playlist_name") or "").strip()
        player = str(schedule.get("player") or "").strip()
        volume = schedule.get("volume")
        details = [
            f"Player: {player}" if player else "",
            f"Media: {media_name}" if media_name else "",
            f"Volume: {volume}%" if volume not in (None, "") else "",
            f"Mode: {schedule.get('media_mode')}" if schedule.get("media_mode") else "",
        ]
        return CalendarEvent(
            summary=name,
            start=event_start,
            end=event_start + timedelta(minutes=30),
            description="\n".join(detail for detail in details if detail),
            uid=f"homeii-flow-{schedule.get('id') or schedule.get('schedule_id')}-{event_start.isoformat()}",
        )
