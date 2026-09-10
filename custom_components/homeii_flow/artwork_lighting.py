"""Persistent, event-driven artwork lighting, independent of dashboard sessions."""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta, datetime, UTC
from io import BytesIO
import logging
import time

from PIL import Image
from homeassistant.components.media_player import DATA_COMPONENT
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

_LOGGER = logging.getLogger(__name__)


def artwork_color(data):
    """Extract a bounded, representative artwork color in an executor."""
    if not data or len(data) > 8 * 1024 * 1024:
        return None
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > 20_000_000:
            return None
        image.thumbnail((48, 48))
        colors = image.convert("RGB").quantize(colors=12).convert("RGB").getcolors(2304) or []
        if not colors:
            return None
        # Prefer a present saturated color without manufacturing color for monochrome art.
        _, color = max(colors, key=lambda entry: entry[0] * (1 + (max(entry[1]) - min(entry[1])) / 128))
        return list(color)


class ArtworkLighting:
    def __init__(self, runtime):
        self.runtime = runtime
        self.hass = runtime.hass
        self._unsub = None
        self._interval = None
        self._tasks = {}
        self._last = {}
        self._attempt = {}
        self.status = {}

    def snapshot(self):
        return {"rules": copy.deepcopy(self.runtime._storage.get("artwork_lighting", {})), "status": copy.deepcopy(self.status)}

    @callback
    def start(self):
        if self._interval is None:
            self._interval = async_track_time_interval(self.hass, self._tick, timedelta(seconds=10))
            @callback
            def on_stop(_):
                self.stop()
            self.hass.bus.async_listen_once("homeassistant_stop", on_stop)
        self._subscribe()
        self._tick(None)

    @callback
    def stop(self):
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._interval:
            self._interval()
            self._interval = None
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    @callback
    def _subscribe(self):
        if self._unsub:
            self._unsub()
            self._unsub = None
        players = list(self.runtime._storage.get("artwork_lighting", {}))
        if players:
            self._unsub = async_track_state_change_event(self.hass, players, self._changed)

    @callback
    def _changed(self, event):
        self._schedule(event.data.get("entity_id"))

    @callback
    def _tick(self, _):
        for player in self.runtime._storage.get("artwork_lighting", {}):
            self._schedule(player)

    @callback
    def _schedule(self, player):
        if player in self._tasks and not self._tasks[player].done():
            return
        self._tasks[player] = self.hass.async_create_task(self._apply(player))

    async def configure(self, payload):
        player = str(payload.get("player") or "").strip()
        if not player.startswith("media_player.") or not self.hass.states.get(player):
            raise ValueError("Choose an existing media player")
        rules = self.runtime._storage.setdefault("artwork_lighting", {})
        existing = rules.get(player, {})
        lights = payload.get("lights", existing.get("lights", []))
        if not isinstance(lights, list) or any(not isinstance(light, str) or not light.startswith("light.") or not self.hass.states.get(light) for light in lights):
            raise ValueError("Choose existing light entities")
        lights = list(dict.fromkeys(lights))
        enabled = payload.get("enabled", existing.get("enabled", False))
        if not isinstance(enabled, bool) or (enabled and not lights):
            raise ValueError("Assign lights before enabling artwork lighting")
        if enabled and any(other != player and rule.get("enabled") and set(lights).intersection(rule.get("lights", [])) for other, rule in rules.items()):
            raise ValueError("A light is already following another player. Disable that mapping first.")
        rule = {"lights": lights, "enabled": enabled}
        for key, default, lower, upper in (("brightness", 35, 1, 100), ("transition", 3, 0, 120), ("cooldown", 8, 1, 120)):
            value = float(payload.get(key, existing.get(key, default)))
            if not lower <= value <= upper:
                raise ValueError(f"Invalid {key}")
            rule[key] = value
        rules[player] = rule
        try:
            await self.runtime.async_save()
        except Exception:
            if existing:
                rules[player] = existing
            else:
                rules.pop(player, None)
            raise
        self._last.pop(player, None)
        self._attempt.pop(player, None)
        self.status[player] = {"state": "waiting" if enabled else "disabled"}
        self._subscribe()
        self._schedule(player)
        return self.snapshot()

    def _signature(self, player, rule):
        state = self.hass.states.get(player)
        if not state or state.state != "playing" or not rule.get("enabled"):
            return None
        attrs = state.attributes
        volume = attrs.get("volume_level")
        volume = float(volume) if isinstance(volume, (int, float)) else 1
        brightness = max(1, round(rule["brightness"] * max(0, min(1, volume))))
        return (attrs.get("media_content_id"), attrs.get("media_title"), attrs.get("entity_picture"), brightness, tuple(rule["lights"]), rule["transition"])

    async def _apply(self, player):
        rule = copy.deepcopy(self.runtime._storage.get("artwork_lighting", {}).get(player, {}))
        signature = self._signature(player, rule)
        if signature is None:
            self._last.pop(player, None)
            self.status[player] = {"state": "waiting" if rule.get("enabled") else "disabled"}
            return
        if self._last.get(player) == signature or time.monotonic() - self._attempt.get(player, 0) < rule["cooldown"]:
            return
        self._attempt[player] = time.monotonic()
        try:
            component = self.hass.data.get(DATA_COMPONENT)
            entity = component.get_entity(player) if component else None
            if entity is None:
                raise ValueError("Player artwork is unavailable")
            async with asyncio.timeout(10):
                data, _ = await entity.async_get_media_image()
            rgb = await self.hass.async_add_executor_job(artwork_color, data)
            if rgb is None:
                raise ValueError("No artwork color available")
            # A track change or disable during image download must not turn lights on.
            current = self.runtime._storage.get("artwork_lighting", {}).get(player, {})
            if current != rule or self._signature(player, current) != signature:
                return
            errors = []
            for light in rule["lights"]:
                if self.runtime._storage.get("artwork_lighting", {}).get(player) != rule or self._signature(player, rule) != signature:
                    return
                state = self.hass.states.get(light)
                modes = state.attributes.get("supported_color_modes", []) if state else []
                if not state or state.state in ("unavailable", "unknown") or not set(modes).intersection({"rgb", "rgbw", "rgbww", "hs", "xy"}):
                    errors.append(light)
                    continue
                try:
                    service = {"entity_id": light, "rgb_color": rgb, "brightness_pct": signature[3]}
                    if state.attributes.get("supported_features", 0) & 32:
                        service["transition"] = rule["transition"]
                    await self.hass.services.async_call("light", "turn_on", service, blocking=True)
                except Exception:
                    errors.append(light)
            self.status[player] = {"state": "partial" if errors else "active", "failed_lights": errors, "rgb": rgb, "updated_at": datetime.now(UTC).isoformat(), "media_title": signature[1]}
            if not errors:
                self._last[player] = signature
        except Exception as err:
            self.status[player] = {"state": "error", "message": str(err)}
            _LOGGER.debug("Artwork lighting for %s could not update: %s", player, err)
