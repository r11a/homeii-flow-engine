"""Verify the narrow queue settings API against live-shaped MA entries."""
import asyncio
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "custom_components/homeii_flow/queue_settings.py"))
validate = module["validate_changes"]
read = module["supported_entries"]
execute = module["async_queue_settings"]
ENTRIES = {
    "autoplay_enabled": {"type": "boolean", "value": True},
    "autoplay_mode": {"type": "string", "value": "auto", "options": [{"value": "auto"}, {"value": "library"}, {"value": "playlist"}]},
    "autoplay_playlist": {"type": "string", "value": None},
    "crossfade_duration": {"type": "integer", "value": 8, "range": [1, 15]},
}

class QueueSettingsTests(unittest.IsolatedAsyncioTestCase):
    def test_partial_changes_preserve_false(self):
        self.assertEqual(validate({"autoplay_enabled": False}, ENTRIES), {"autoplay_enabled": False})

    def test_invalid_values_and_unrelated_settings_are_rejected(self):
        for patch in ({"autoplay_enabled": "false"}, {"crossfade_duration": True}, {"crossfade_duration": 16}, {"autoplay_mode": "invented"}, {"password": "secret"}, {"smart_shuffle_enabled": "enabled"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate(patch, ENTRIES)

    def test_playlist_mode_requires_a_playlist(self):
        with self.assertRaises(ValueError):
            validate({"autoplay_mode": "playlist"}, ENTRIES)
        self.assertEqual(validate({"autoplay_mode": "playlist", "autoplay_playlist": "library://playlist/1"}, ENTRIES)["autoplay_mode"], "playlist")

    def test_only_supported_public_fields_are_returned(self):
        result = read({"values": {**ENTRIES, "secret": {"value": "private"}}})
        self.assertNotIn("secret", result)
        self.assertNotIn("credentials", result["autoplay_enabled"])

    async def test_save_is_partial_and_read_back(self):
        client = SimpleNamespace(async_command=AsyncMock(side_effect=[{"values": ENTRIES}, None, {"values": {**ENTRIES, "autoplay_enabled": {"type": "boolean", "value": False}}}]))
        result = await execute(client, {"autoplay_enabled": False})
        self.assertTrue(result["saved"])
        self.assertEqual(client.async_command.call_args_list[1].args, ("config/core/save", {"domain": "player_queues", "values": {"autoplay_enabled": False}}))

    async def test_unconfirmed_save_fails(self):
        client = SimpleNamespace(async_command=AsyncMock(side_effect=[{"values": ENTRIES}, None, {"values": ENTRIES}]))
        with self.assertRaises(ValueError):
            await execute(client, {"autoplay_enabled": False})

    async def test_timeout_does_not_retry_a_write(self):
        client = SimpleNamespace(async_command=AsyncMock(side_effect=[{"values": ENTRIES}, asyncio.TimeoutError()]))
        with self.assertRaises(asyncio.TimeoutError):
            await execute(client, {"autoplay_enabled": False})
        self.assertEqual(client.async_command.await_count, 2)

    async def test_read_never_writes(self):
        client = SimpleNamespace(async_command=AsyncMock(return_value={"values": ENTRIES}))
        self.assertFalse((await execute(client))["saved"])
        self.assertEqual(client.async_command.await_count, 1)

class QueueSettingsPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_admin_can_write_but_authenticated_users_can_read(self):
        import ast
        from unittest.mock import Mock
        path = Path(__file__).resolve().parents[1] / "custom_components/homeii_flow/websocket_api.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "websocket_queue_settings")
        function.decorator_list = []
        runtime = SimpleNamespace(async_queue_settings=AsyncMock(return_value={"entries": {}}))
        ns = {"_runtime": lambda hass: runtime, "_command_payload": lambda msg: msg}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(path), "exec"), ns)
        connection = SimpleNamespace(user=SimpleNamespace(is_admin=False), send_error=Mock(), send_result=Mock())
        await ns["websocket_queue_settings"](None, connection, {"id": 1, "values": {"autoplay_enabled": False}})
        runtime.async_queue_settings.assert_not_awaited()
        self.assertEqual(connection.send_error.call_args.args[1], "unauthorized")
        await ns["websocket_queue_settings"](None, connection, {"id": 2})
        self.assertFalse(connection.send_result.call_args.args[1]["can_edit"])
        connection.user.is_admin = True
        await ns["websocket_queue_settings"](None, connection, {"id": 3, "values": {"autoplay_enabled": False}})
        self.assertTrue(connection.send_result.call_args.args[1]["can_edit"])
