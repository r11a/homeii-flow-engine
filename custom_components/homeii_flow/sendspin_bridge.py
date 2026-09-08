"""Authenticated HA-to-MA Sendspin transport; MA credentials stay on the server."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit, urlunsplit

from aiohttp import WSMsgType, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN


async def relay_frames(source, destination) -> None:
    """Forward streaming frames with backpressure and stop when either side closes."""
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
            break


class HomeiiFlowSendspinView(HomeAssistantView):
    """Use HA authentication, including its short-lived signed GET paths."""

    url = "/api/homeii_flow/sendspin/{client_id}"
    name = "api:homeii_flow:sendspin"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request: web.Request, client_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", client_id):
            raise web.HTTPBadRequest(text="Invalid player id")
        runtime = self.hass.data[DOMAIN]["runtime"]
        urls = runtime.music_assistant_base_urls()
        tokens = runtime.music_assistant_tokens()
        if not urls or not tokens:
            raise web.HTTPServiceUnavailable(text="Configure Music Assistant in HOMEii Flow Engine")
        parts = urlsplit(urls[0])
        upstream_url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                                   parts.path.rstrip("/") + "/sendspin", "", ""))
        session = async_get_clientsession(self.hass)
        upstream = None
        try:
            async with asyncio.timeout(12):
                upstream = await session.ws_connect(upstream_url, heartbeat=30, max_msg_size=4 * 1024 * 1024)
                await upstream.send_json({"type": "auth", "token": tokens[0], "client_id": client_id})
                auth = await upstream.receive_json()
                if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
                    raise web.HTTPBadGateway(text="Music Assistant rejected the Sendspin connection")
        except asyncio.CancelledError:
            if upstream is not None:
                await upstream.close()
            raise
        except Exception as error:
            if upstream is not None:
                await upstream.close()
            if isinstance(error, web.HTTPException):
                raise
            raise web.HTTPBadGateway(text="Music Assistant Sendspin is unavailable") from None

        downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
        tasks = []
        try:
            await downstream.prepare(request)
            await downstream.send_json({"type": "auth_ok"})
            tasks = [asyncio.create_task(relay_frames(upstream, downstream)),
                     asyncio.create_task(relay_frames(downstream, upstream))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(upstream.close(), downstream.close(), return_exceptions=True)
        return downstream
