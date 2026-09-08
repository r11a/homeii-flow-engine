"""Exercise actual Sendspin view and relay with simulated HA/MA boundaries."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock
import re
from urllib.parse import urlsplit, urlunsplit

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/homeii_flow/sendspin_bridge.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
class HTTPError(Exception):
    def __init__(self, text=''): super().__init__(text)
class Socket:
    def __init__(self, frames=()):
        self.frames = frames
        self.send_str = AsyncMock()
        self.send_bytes = AsyncMock()
        self.send_json = AsyncMock()
        self.close = AsyncMock()
        self.prepare = AsyncMock()
        self.receive_json = AsyncMock(return_value={'type':'auth_ok'})
    def __aiter__(self):
        async def iterate():
            for frame in self.frames: yield frame
        return iterate()

kinds = SimpleNamespace(TEXT=1, BINARY=2, ERROR=3, CLOSE=4, CLOSED=5)
web = SimpleNamespace(HTTPException=HTTPError, HTTPBadRequest=HTTPError, HTTPBadGateway=HTTPError, HTTPServiceUnavailable=HTTPError)
ns = dict(asyncio=asyncio, re=re, urlsplit=urlsplit, urlunsplit=urlunsplit, DOMAIN='homeii_flow', WSMsgType=kinds, web=web, HomeAssistantView=object)
nodes = [ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]
nodes += [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef))]
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(SOURCE),'exec'),ns)

class BridgeTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.up = Socket()
        self.down = Socket()
        web.WebSocketResponse = lambda **_: self.down
        ns['async_get_clientsession'] = lambda _: SimpleNamespace(ws_connect=AsyncMock(return_value=self.up))
        runtime = SimpleNamespace(music_assistant_base_urls=lambda:['http://ma:8095'],music_assistant_tokens=lambda:['server-secret'])
        self.view = ns['HomeiiFlowSendspinView'](SimpleNamespace(data={'homeii_flow':{'runtime':runtime}}))
    async def test_text_binary_forwarding_stops_on_close(self):
        source=Socket([SimpleNamespace(type=kinds.TEXT,data='hello'),SimpleNamespace(type=kinds.BINARY,data=b'audio'),SimpleNamespace(type=kinds.CLOSE,data=None),SimpleNamespace(type=kinds.TEXT,data='late')])
        await ns['relay_frames'](source,self.down)
        self.down.send_str.assert_awaited_once_with('hello')
        self.down.send_bytes.assert_awaited_once_with(b'audio')
    async def test_success_authenticates_upstream_only_and_closes_both(self):
        self.assertTrue(self.view.requires_auth)
        await self.view.get(object(),'homeii-test')
        self.up.send_json.assert_awaited_once_with({'type':'auth','token':'server-secret','client_id':'homeii-test'})
        self.down.send_json.assert_awaited_once_with({'type':'auth_ok'})
        self.up.close.assert_awaited_once()
        self.down.close.assert_awaited_once()
    async def test_rejection_never_upgrades_client_connection(self):
        self.up.receive_json.return_value={'type':'auth_invalid'}
        with self.assertRaises(HTTPError): await self.view.get(object(),'device')
        self.down.prepare.assert_not_awaited()
        self.up.close.assert_awaited_once()
    async def test_handshake_cancellation_closes_upstream(self):
        self.up.receive_json.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError): await self.view.get(object(),'device')
        self.up.close.assert_awaited_once()
    async def test_relay_failure_still_closes_both(self):
        self.up.frames=[SimpleNamespace(type=kinds.TEXT,data='frame')]
        self.down.send_str.side_effect=ValueError('transport failure')
        with self.assertRaises(ValueError): await self.view.get(object(),'device')
        self.up.close.assert_awaited_once()
        self.down.close.assert_awaited_once()
    async def test_rejects_invalid_client_path(self):
        with self.assertRaises(HTTPError): await self.view.get(object(),'../invalid')
        self.up.send_json.assert_not_awaited()

if __name__ == '__main__': main()