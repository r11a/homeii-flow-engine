"""HOMEii Flow Engine integration."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import voluptuous as vol

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.service import async_register_admin_service

from .sendspin_bridge import HomeiiFlowSendspinView

from .const import (
    CONF_ENABLE_EXPERIMENTAL,
    CONF_INSTANCE_ID,
    CONF_MUSIC_ASSISTANT_EXTERNAL_URL,
    CONF_MUSIC_ASSISTANT_TOKEN,
    CONF_MUSIC_ASSISTANT_URL,
    CONF_PROFILE_ID,
    DEFAULT_INSTANCE_ID,
    DEFAULT_PROFILE_ID,
    DOMAIN,
    PLATFORMS,
)
from .queue_settings import FIELD_TYPES
from .runtime import HomeiiFlowRuntime
from .websocket_api import async_register_websocket_commands

_LOGGER = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).parent / "frontend"

SERVICE_SET_VOLUME_RULE = "set_volume_rule"
SERVICE_DELETE_VOLUME_RULE = "delete_volume_rule"
SERVICE_CLEAR_VOLUME_RULES = "clear_volume_rules"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_DELETE_SCHEDULE = "delete_schedule"
SERVICE_RUN_SCHEDULE = "run_schedule"
SERVICE_SET_TIMER = "set_timer"
SERVICE_DELETE_TIMER = "delete_timer"
SERVICE_ANNOUNCE = "announce"
SERVICE_PLAY_MEDIA = "play_media"
SERVICE_PLAYER_COMMAND = "player_command"
SERVICE_SET_QUEUE_SETTINGS = "set_queue_settings"
SERVICE_SET_QUEUE_SETTINGS_SCHEMA = vol.Schema({vol.Optional(key): kind for key, kind in FIELD_TYPES.items()})
SERVICE_TRANSFER_QUEUE = "transfer_queue"
SERVICE_RUN_ORCHESTRATION = "run_orchestration"
SERVICE_SET_SCREENSAVER = "set_screensaver"
SERVICE_SHOW_SCREENSAVER = "show_screensaver"

SERVICE_SET_VOLUME_RULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
        vol.Required("max_volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("start_time", default=""): str,
        vol.Optional("end_time", default=""): str,
        vol.Optional("days", default=list): [int],
        vol.Optional("enabled", default=True): bool,
    }
)

SERVICE_CLEAR_VOLUME_RULES_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
    }
)

SERVICE_DELETE_VOLUME_RULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
    }
)

SERVICE_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("id"): str,
        vol.Optional("schedule_id"): str,
        vol.Optional("name", default="HOMEii schedule"): str,
        vol.Optional("kind", default="wake_playback"): str,
        vol.Optional("action", default="wake_playback"): str,
        vol.Required("player"): str,
        vol.Optional("media_id", default=""): str,
        vol.Optional("media_content_id", default=""): str,
        vol.Optional("playlist", default=""): str,
        vol.Optional("media_type", default="music"): str,
        vol.Optional("media_content_type", default=""): str,
        vol.Optional("media_name"): str,
        vol.Optional("playlist_name"): str,
        vol.Optional("media_mode"): str,
        vol.Optional("selection_mode"): str,
        vol.Optional("enqueue", default="play"): str,
        vol.Optional("radio_mode", default=False): bool,
        vol.Optional("retry_attempts", default=4): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
        vol.Optional("retry_delay", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
        vol.Required("time"): str,
        vol.Optional("days", default=list): [int],
        vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("enabled", default=True): bool,
        vol.Optional("after_run", default="keep"): str,
    }
)

SERVICE_DELETE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("id"): str,
        vol.Optional("schedule_id"): str,
    }
)

SERVICE_RUN_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("id"): str,
        vol.Optional("schedule_id"): str,
    }
)

SERVICE_SET_TIMER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("id"): str,
        vol.Optional("timer_id"): str,
        vol.Required("player"): str,
        vol.Optional("action", default="stop"): str,
        vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
        vol.Optional("ends_at"): str,
        vol.Optional("origin"): str,
        vol.Optional("enabled", default=True): bool,
    }
)

SERVICE_DELETE_TIMER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("id"): str,
        vol.Optional("timer_id"): str,
        vol.Optional("player"): str,
    }
)

SERVICE_ANNOUNCE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("message"): str,
        vol.Optional("player"): str,
        vol.Optional("players", default=list): [str],
        vol.Optional("entity_id"): str,
        vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("language"): str,
        vol.Optional("tts_entity"): str,
        vol.Optional("announcement_tts_entity"): str,
        vol.Optional("target"): str,
    }
)

SERVICE_PLAY_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
        vol.Required("media_id"): str,
        vol.Optional("media_type", default="music"): str,
        vol.Optional("enqueue", default="play"): str,
        vol.Optional("radio_mode", default=False): bool,
    }
)

SERVICE_PLAYER_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
        vol.Required("command"): str,
        vol.Optional("volume"): vol.Any(int, float),
        vol.Optional("volume_level"): vol.Any(int, float),
        vol.Optional("shuffle", default=True): bool,
        vol.Optional("is_volume_muted"): bool,
        vol.Optional("autoplay_enabled"): bool,
        vol.Optional("crossfade_enabled"): bool,
        vol.Optional("seek_position"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("speed"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=3.0)),
    }
)

SERVICE_TRANSFER_QUEUE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("source_player"): str,
        vol.Required("target_player"): str,
        vol.Optional("auto_play", default=True): bool,
    }
)

SERVICE_SET_SCREENSAVER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("enabled"): bool,
        vol.Optional("timeout_seconds"): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
        vol.Optional("mode"): vol.In(["auto", "clock", "lyrics"]),
        vol.Optional("auto_lyrics_when_playing"): bool,
        vol.Optional("clock_mode"): vol.In(["digital", "analog"]),
        vol.Optional("message"): str,
        vol.Optional("show_artwork"): bool,
        vol.Optional("music_assistant_url"): str,
        vol.Optional("ma_url"): str,
        vol.Optional("music_assistant_external_url"): str,
    }
)

SERVICE_SHOW_SCREENSAVER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("source", default="service"): str,
    }
)


def _looks_like_artwork_url(value: str) -> bool:
    """Return whether a string looks like an artwork URL/path."""
    clean = value.strip()
    if not clean:
        return False
    lower = clean.lower()
    return (
        lower.startswith(("http://", "https://", "/", "data:image/"))
        or "imageproxy" in lower
        or "media_player_proxy" in lower
        or lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
    )


def _append_artwork_candidate(candidates: list[str], value: Any) -> None:
    """Append a unique artwork candidate."""
    if isinstance(value, dict):
        for key in ("url", "path", "src", "uri", "image", "thumbnail", "thumb"):
            _append_artwork_candidate(candidates, value.get(key))
        return
    if isinstance(value, list):
        for item in value:
            _append_artwork_candidate(candidates, item)
        return
    clean = str(value or "").strip()
    ignored = {"builtin", "jpeg", "jpg", "png", "webp", "gif", "image", "images", "default"}
    lower = clean.lower()
    if clean and clean not in candidates and (_looks_like_artwork_url(clean) or (len(clean) > 5 and lower not in ignored)):
        candidates.append(clean)


def _collect_artwork_candidates(value: Any, candidates: list[str], *, depth: int = 0, art_context: bool = False) -> None:
    """Collect artwork candidates from common Music Assistant response shapes."""
    if value is None or depth > 8:
        return
    if isinstance(value, str):
        if art_context:
            _append_artwork_candidate(candidates, value)
        return
    if isinstance(value, list):
        for item in value[:40]:
            _collect_artwork_candidates(item, candidates, depth=depth + 1, art_context=art_context)
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        clean_key = str(key or "").lower()
        is_art_key = any(
            token in clean_key
            for token in ("art", "cover", "image", "thumbnail", "thumb", "picture", "fanart")
        )
        if is_art_key:
            _append_artwork_candidate(candidates, child)
        _collect_artwork_candidates(child, candidates, depth=depth + 1, art_context=art_context or is_art_key)


class HomeiiFlowArtworkProxyView(HomeAssistantView):
    """Proxy current player artwork through Home Assistant for dashboard agents."""

    url = "/api/homeii_flow/artwork/{entity_id}"
    name = "api:homeii_flow:artwork"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the artwork proxy."""
        self.hass = hass

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        """Return current artwork for a media player entity."""
        runtime = async_get_runtime(self.hass)
        player = next(
            (
                item
                for item in runtime.media_players_snapshot()
                if str(item.get("entity_id") or "").strip() == entity_id
            ),
            None,
        )
        if not player:
            raise web.HTTPNotFound(text="player artwork source not found")

        candidates = [
            *[
                str(item or "").strip()
                for item in player.get("artwork_candidates", [])
                if str(item or "").strip()
            ],
            str(player.get("entity_picture") or "").strip(),
            str(player.get("media_image_url") or "").strip(),
        ]
        try:
            queue_response = await runtime.async_get_queue(
                {
                    "entity_id": entity_id,
                    "queue_id": str(player.get("active_queue") or "").strip(),
                    "include_diagnostics": True,
                }
            )
            _collect_artwork_candidates(queue_response.get("data"), candidates)
        except Exception:  # noqa: BLE001 - artwork proxy must remain best effort
            pass
        unique_candidates = [item for index, item in enumerate(candidates) if item and item not in candidates[:index]]
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=8)
        ma_base_urls = runtime.music_assistant_base_urls()
        ma_tokens = runtime.music_assistant_tokens()
        for source in unique_candidates:
            for url in self._absolute_artwork_urls(request, source, ma_base_urls):
                try:
                    headers = {"Accept": "image/*,*/*;q=0.8"}
                    if ma_tokens and any(
                        url.startswith(f"{base.rstrip('/')}/") for base in ma_base_urls
                    ):
                        headers["Authorization"] = f"Bearer {ma_tokens[0]}"
                    async with session.get(url, timeout=timeout, headers=headers) as response:
                        if response.status >= 400:
                            continue
                        body = await response.read()
                        if not body:
                            continue
                        content_type = response.headers.get("Content-Type") or "image/jpeg"
                        return web.Response(
                            body=body,
                            content_type=content_type.split(";", 1)[0],
                            headers={
                                "Cache-Control": "no-store, max-age=0",
                                "X-HOMEii-Flow-Artwork-Source": "proxy",
                            },
                        )
                except (ClientError, TimeoutError, ValueError):
                    continue
        raise web.HTTPNotFound(text="artwork could not be loaded")

    @staticmethod
    def _absolute_artwork_urls(request: web.Request, source: str, ma_base_urls: list[str]) -> list[str]:
        """Return absolute artwork URLs that Home Assistant can fetch."""
        if source.startswith("data:") or source.startswith("blob:"):
            return []
        if source.startswith("//"):
            return [f"{request.scheme}:{source}"]
        if source.startswith("http://") or source.startswith("https://"):
            candidates = [source]
            parsed = urlparse(source)
            path_index = parsed.path.find("/imageproxy")
            imageproxy_path = parsed.path[path_index:] if path_index >= 0 else ""
            if imageproxy_path:
                suffix = f"{imageproxy_path}{f'?{parsed.query}' if parsed.query else ''}"
                for base_url in ma_base_urls:
                    candidates.append(f"{base_url.rstrip('/')}{suffix}")
            if parsed.path.rstrip("/").endswith("/imageproxy"):
                query = parse_qs(parsed.query)
                provider = str((query.get("provider") or ["builtin"])[0] or "builtin")
                raw_path = str((query.get("path") or [""])[0] or "")
                image_path = unquote(unquote(raw_path))
                if image_path:
                    image_id = hashlib.sha256(
                        f"{provider}/{image_path}".encode("utf-8"),
                        usedforsecurity=False,
                    ).hexdigest()
                    candidates.append(
                        f"{parsed.scheme}://{parsed.netloc}/imageproxy/{image_id}?size=512"
                    )
                    for base_url in ma_base_urls:
                        candidates.append(f"{base_url.rstrip('/')}/imageproxy/{image_id}?size=512")
            return [candidate for index, candidate in enumerate(candidates) if candidate not in candidates[:index]]
        candidates: list[str] = []
        if source.startswith("/imageproxy") or source.startswith("imageproxy"):
            path = source if source.startswith("/") else f"/{source}"
            for base_url in ma_base_urls:
                candidates.append(f"{base_url.rstrip('/')}{path}")
        if source.startswith("/"):
            candidates.append(f"{request.scheme}://{request.host}{source}")
        else:
            for base_url in ma_base_urls:
                candidates.append(f"{base_url.rstrip('/')}/imageproxy?path={quote(source)}&size=512")
            candidates.append(f"{request.scheme}://{request.host}/{quote(source.lstrip('/'))}")
        return [candidate for index, candidate in enumerate(candidates) if candidate and candidate not in candidates[:index]]


class HomeiiFlowItemArtworkProxyView(HomeiiFlowArtworkProxyView):
    """Proxy registered queue/library artwork through Home Assistant."""

    url = "/api/homeii_flow/artwork/item/{token}"
    name = "api:homeii_flow:item_artwork"
    requires_auth = False

    @staticmethod
    def _artwork_response(
        request: web.Request,
        body: bytes,
        content_type: str,
        source_label: str,
    ) -> web.Response:
        """Return cache-friendly artwork with conditional request support."""
        etag = f'"{hashlib.sha256(body).hexdigest()}"'
        headers = {
            "Cache-Control": "private, max-age=1800, stale-while-revalidate=86400",
            "ETag": etag,
            "X-HOMEii-Flow-Artwork-Source": source_label,
        }
        if request.headers.get("If-None-Match", "").strip() == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(
            body=body,
            content_type=str(content_type or "image/jpeg").split(";", 1)[0],
            headers=headers,
        )

    async def get(self, request: web.Request, token: str) -> web.Response:
        """Return artwork for an opaque Engine token."""
        runtime = async_get_runtime(self.hass)
        source = runtime.resolve_artwork_source(token)
        if not source:
            raise web.HTTPNotFound(text="artwork token not found or expired")
        cached = runtime.cached_artwork_content(source)
        if cached:
            body, content_type = cached
            return self._artwork_response(request, body, content_type, "memory-cache")
        ma_base_urls = runtime.music_assistant_base_urls()
        ma_tokens = runtime.music_assistant_tokens()
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(total=8)
        for url in self._absolute_artwork_urls(request, source, ma_base_urls):
            try:
                headers = {"Accept": "image/*,*/*;q=0.8"}
                if ma_tokens and any(
                    url.startswith(f"{base.rstrip('/')}/") for base in ma_base_urls
                ):
                    headers["Authorization"] = f"Bearer {ma_tokens[0]}"
                async with session.get(url, timeout=timeout, headers=headers) as response:
                    if response.status >= 400:
                        continue
                    body = await response.read()
                    if not body:
                        continue
                    content_type = response.headers.get("Content-Type") or "image/jpeg"
                    runtime.cache_artwork_content(source, body, content_type)
                    return self._artwork_response(request, body, content_type, "item-proxy")
            except (ClientError, TimeoutError, ValueError):
                continue
        raise web.HTTPNotFound(text="artwork could not be loaded")


class HomeiiFlowCommandView(HomeAssistantView):
    """Expose selected Engine reads over authenticated HTTP for frontend fallbacks."""

    url = "/api/homeii_flow/command/{command:.+}"
    name = "api:homeii_flow:command"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the command view."""
        self.hass = hass

    async def post(self, request: web.Request, command: str) -> web.Response:
        """Run a read command for clients that cannot use the websocket command path."""
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON should become a clear HTTP error
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        runtime = async_get_runtime(self.hass)
        clean_command = str(command or "").strip().strip("/")
        instance_id = str(payload.get(CONF_INSTANCE_ID) or "").strip() or None
        profile_id = str(payload.get(CONF_PROFILE_ID) or "").strip() or None
        if clean_command == "get_context":
            result = runtime.context(instance_id=instance_id, profile_id=profile_id)
        elif clean_command == "bootstrap/get":
            result = runtime.bootstrap_snapshot(instance_id=instance_id, profile_id=profile_id)
        elif clean_command == "queue/get":
            result = await runtime.async_get_queue(payload)
        elif clean_command == "library/get":
            result = await runtime.async_get_library(payload)
        elif clean_command == "favorites/get":
            result = await runtime.async_get_favorites(payload)
        elif clean_command == "favorites/set":
            result = await runtime.async_set_favorite(payload)
        elif clean_command == "search/get":
            result = await runtime.async_get_search(payload)
        elif clean_command == "ma/command":
            result = await runtime.async_music_assistant_command(payload)
        else:
            raise web.HTTPNotFound(text="unsupported HOMEii Flow Engine command")
        return web.json_response(result)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up HOMEii Flow Engine."""
    await async_prepare_runtime(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HOMEii Flow Engine from a config entry."""
    runtime = await async_prepare_runtime(hass)
    instance_id = str(entry.data.get(CONF_INSTANCE_ID) or DEFAULT_INSTANCE_ID)
    profile_id = str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)
    enable_experimental = bool(entry.options.get(CONF_ENABLE_EXPERIMENTAL, False))
    music_assistant_url = str(
        entry.options.get(CONF_MUSIC_ASSISTANT_URL)
        or entry.data.get(CONF_MUSIC_ASSISTANT_URL)
        or ""
    ).strip()
    music_assistant_external_url = str(
        entry.options.get(CONF_MUSIC_ASSISTANT_EXTERNAL_URL)
        or entry.data.get(CONF_MUSIC_ASSISTANT_EXTERNAL_URL)
        or ""
    ).strip()
    music_assistant_token = str(
        entry.options.get(CONF_MUSIC_ASSISTANT_TOKEN)
        or entry.data.get(CONF_MUSIC_ASSISTANT_TOKEN)
        or ""
    ).strip()
    runtime.register_entry(
        entry.entry_id,
        instance_id=instance_id,
        profile_id=profile_id,
        title=entry.title,
        enable_experimental=enable_experimental,
        music_assistant_url=music_assistant_url,
        music_assistant_external_url=music_assistant_external_url,
        music_assistant_token=music_assistant_token,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a HOMEii Flow Engine config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    runtime = async_get_runtime(hass)
    runtime.unregister_entry(entry.entry_id)
    return True


async def _async_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    await hass.config_entries.async_reload(entry.entry_id)


def async_get_runtime(hass: HomeAssistant) -> HomeiiFlowRuntime:
    """Return the HOMEii Flow runtime."""
    data = hass.data.setdefault(DOMAIN, {})
    runtime = data.get("runtime")
    if isinstance(runtime, HomeiiFlowRuntime):
        return runtime
    runtime = HomeiiFlowRuntime(hass)
    data["runtime"] = runtime
    return runtime


async def async_prepare_runtime(hass: HomeAssistant) -> HomeiiFlowRuntime:
    """Return a loaded runtime with Engine services and websocket commands registered."""
    data = hass.data.setdefault(DOMAIN, {})
    runtime = async_get_runtime(hass)
    if not data.get("runtime_loaded"):
        await runtime.async_load()
        data["runtime_loaded"] = True
    runtime.async_start_orchestration()
    if not data.get("websocket_registered"):
        async_register_websocket_commands(hass)
        data["websocket_registered"] = True
    if not data.get("frontend_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig("/homeii_flow", str(FRONTEND_DIR), cache_headers=False)]
        )
        data["frontend_registered"] = True
    if not data.get("artwork_proxy_registered"):
        hass.http.register_view(HomeiiFlowArtworkProxyView(hass))
        hass.http.register_view(HomeiiFlowItemArtworkProxyView(hass))
        hass.http.register_view(HomeiiFlowCommandView(hass))
        hass.http.register_view(HomeiiFlowSendspinView(hass))
        data["artwork_proxy_registered"] = True
    if not data.get("services_registered"):
        _async_register_services(hass)
        data["services_registered"] = True
    _LOGGER.debug("HOMEii Flow Engine runtime prepared")
    return runtime


def _async_register_services(hass: HomeAssistant) -> None:
    """Register optional automation-facing services."""

    async def set_volume_rule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_volume_rule(dict(call.data))

    async def clear_volume_rules(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_clear_volume_rules(str(call.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID))

    async def delete_volume_rule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_delete_volume_rule(dict(call.data))

    async def set_schedule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_schedule(dict(call.data))

    async def delete_schedule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_delete_schedule(dict(call.data))

    async def run_schedule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_run_schedule_now(dict(call.data))

    async def set_timer(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_timer(dict(call.data))

    async def delete_timer(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_delete_timer(dict(call.data))

    async def announce(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_send_announcement(dict(call.data))

    async def play_media(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_play_media(dict(call.data))

    async def set_queue_settings(call: ServiceCall) -> None:
        await async_get_runtime(hass).async_queue_settings({"values": dict(call.data)})

    async def player_command(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        try:
            await runtime.async_player_command(dict(call.data))
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
        except RuntimeError as error:
            raise HomeAssistantError(str(error)) from error

    async def transfer_queue(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_transfer_queue(dict(call.data))

    async def run_orchestration(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_tick_orchestration()

    async def set_screensaver(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_screensaver_config(dict(call.data))

    async def show_screensaver(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_request_screensaver_show(dict(call.data))

    if not hass.services.has_service(DOMAIN, SERVICE_SET_VOLUME_RULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_VOLUME_RULE,
            set_volume_rule,
            schema=SERVICE_SET_VOLUME_RULE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_VOLUME_RULES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_VOLUME_RULES,
            clear_volume_rules,
            schema=SERVICE_CLEAR_VOLUME_RULES_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_DELETE_VOLUME_RULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DELETE_VOLUME_RULE,
            delete_volume_rule,
            schema=SERVICE_DELETE_VOLUME_RULE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SCHEDULE,
            set_schedule,
            schema=SERVICE_SET_SCHEDULE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_DELETE_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DELETE_SCHEDULE,
            delete_schedule,
            schema=SERVICE_DELETE_SCHEDULE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_SCHEDULE,
            run_schedule,
            schema=SERVICE_RUN_SCHEDULE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_TIMER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_TIMER,
            set_timer,
            schema=SERVICE_SET_TIMER_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_DELETE_TIMER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DELETE_TIMER,
            delete_timer,
            schema=SERVICE_DELETE_TIMER_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_ANNOUNCE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ANNOUNCE,
            announce,
            schema=SERVICE_ANNOUNCE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_PLAY_MEDIA):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PLAY_MEDIA,
            play_media,
            schema=SERVICE_PLAY_MEDIA_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_QUEUE_SETTINGS):
        async_register_admin_service(hass, DOMAIN, SERVICE_SET_QUEUE_SETTINGS, set_queue_settings, schema=SERVICE_SET_QUEUE_SETTINGS_SCHEMA)
    if not hass.services.has_service(DOMAIN, SERVICE_PLAYER_COMMAND):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PLAYER_COMMAND,
            player_command,
            schema=SERVICE_PLAYER_COMMAND_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_TRANSFER_QUEUE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_TRANSFER_QUEUE,
            transfer_queue,
            schema=SERVICE_TRANSFER_QUEUE_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_ORCHESTRATION):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_ORCHESTRATION,
            run_orchestration,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCREENSAVER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SCREENSAVER,
            set_screensaver,
            schema=SERVICE_SET_SCREENSAVER_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SHOW_SCREENSAVER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SHOW_SCREENSAVER,
            show_screensaver,
            schema=SERVICE_SHOW_SCREENSAVER_SCHEMA,
        )
