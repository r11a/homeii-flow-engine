"""Validate the HTTP response contract without requiring a running HA instance."""
import ast
from pathlib import Path
from typing import Any
from unittest import IsolatedAsyncioTestCase

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/homeii_flow/config_flow.py"
node = next(n for n in ast.parse(SOURCE.read_text(encoding="utf-8")).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_validate_music_assistant_api")

class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs): return self.payload

class ConnectionTests(IsolatedAsyncioTestCase):
    async def validate(self, payload, status=200):
        class Session:
            def get(self, *args, **kwargs): return Response({"schema_version": 999})
            def post(self, *args, **kwargs): return Response(payload, status)
        ns = dict(Any=Any, ClientError=ConnectionError, ClientTimeout=lambda **kw: kw,
                  MUSIC_ASSISTANT_SCHEMA_MIN=1, async_get_clientsession=lambda hass: Session())
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ns)
        return await ns["_validate_music_assistant_api"](None, "http://ma:8095", "test-token")
    async def test_direct_http_player_list(self):
        self.assertIsNone(await self.validate([{"player_id": "computer"}]))
    async def test_empty_player_list_is_valid_connection(self):
        self.assertIsNone(await self.validate([]))
    async def test_wrapped_response_is_supported(self):
        self.assertIsNone(await self.validate({"result": []}))
    async def test_unrelated_json_is_rejected(self):
        for payload in ({}, {"status": "ok"}, None, "html", ["invalid"]):
            self.assertEqual(await self.validate(payload), "invalid_response")
    async def test_authentication_failure_remains_rejected(self):
        for status in (401, 403):
            self.assertEqual(await self.validate([], status), "invalid_auth")
        self.assertEqual(await self.validate({"error": "Authentication failed"}), "invalid_auth")
