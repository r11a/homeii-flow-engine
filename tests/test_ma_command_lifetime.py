"""Exercise the real MA command method with isolated transport boundaries."""
from __future__ import annotations
import ast
import asyncio
from pathlib import Path
import secrets
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/homeii_flow/ma_client.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MusicAssistantEventClient')
cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'async_command']
ns = dict(asyncio=asyncio, secrets=secrets, time=time)
module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), ns)

class CommandLifetimeTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = ns['MusicAssistantEventClient']()
        self.client._authenticated = True
        self.client._ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        self.client._send_lock = asyncio.Lock()
        self.client._pending_commands = {}
        self.client._partial_results = {}

    async def test_lock_wait_respects_deadline_without_sending(self):
        await self.client._send_lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, 'command timed out'):
                await asyncio.wait_for(self.client.async_command('players/cmd/play', timeout=.02), .5)
            self.client._ws.send_json.assert_not_awaited()
            self.assertFalse(self.client._pending_commands)
            self.assertFalse(self.client._partial_results)
        finally:
            self.client._send_lock.release()

    async def test_send_stall_respects_deadline_and_cancels_future(self):
        futures = []
        async def blocked_send(payload):
            futures.append(self.client._pending_commands[payload['message_id']])
            await asyncio.Event().wait()
        self.client._ws.send_json.side_effect = blocked_send
        with self.assertRaisesRegex(RuntimeError, 'command timed out'):
            await asyncio.wait_for(self.client.async_command('players/cmd/play', timeout=.02), .5)
        self.assertTrue(futures[0].cancelled())
        self.assertFalse(self.client._pending_commands)

    async def test_success_returns_response_and_cleans_up(self):
        async def respond(payload):
            self.client._pending_commands[payload['message_id']].set_result({'ok': True})
        self.client._ws.send_json.side_effect = respond
        self.assertEqual(await self.client.async_command('players/all'), {'ok': True})
        self.assertFalse(self.client._pending_commands)
        self.assertFalse(self.client._partial_results)

    async def test_caller_cancellation_cleans_up_pending_future(self):
        started = asyncio.Event()
        futures = []
        async def sent(payload):
            futures.append(self.client._pending_commands[payload['message_id']])
            started.set()
        self.client._ws.send_json.side_effect = sent
        task = asyncio.create_task(self.client.async_command('players/all'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(futures[0].cancelled())
        self.assertFalse(self.client._pending_commands)

    async def test_expired_command_is_never_sent(self):
        with self.assertRaisesRegex(RuntimeError, 'command timed out'):
            await self.client.async_command('players/cmd/play', timeout=0)
        self.client._ws.send_json.assert_not_awaited()
        self.assertFalse(self.client._pending_commands)
