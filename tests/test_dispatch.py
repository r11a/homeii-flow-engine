"""Regression checks for actual announcement and transfer methods."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

source = Path(__file__).resolve().parents[1] / "custom_components/homeii_flow/runtime.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowRuntime")
cls.decorator_list = []
cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {"async_send_announcement", "async_transfer_queue", "async_player_command", "async_apply_group", "async_execute_timer", "_async_execute_timer_once", "async_queue_action"}]
module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
namespace = {"asyncio": asyncio,"tts": SimpleNamespace(generate_media_source_id=Mock(return_value="media-source://tts/test")), "_safe_list": lambda value: value if isinstance(value, list) else [], "_utc_iso": lambda: "now", "EVENT_ENGINE_GROUP_APPLY": "group", "SIGNAL_ENGINE_UPDATED": "update", "async_dispatcher_send": lambda *args: None, "DEFAULT_PROFILE_ID": "default", "HomeiiFlowServiceUnavailable": RuntimeError, "_clean_string": lambda v: str(v or "").strip(), "_dict_first": lambda d, *ks: next((d[k] for k in ks if d.get(k)), None)}
exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
import runpy
namespace["build_queue_switch"] = runpy.run_path(str(source.parent / "queue_controls.py"))["build_queue_switch"]
namespace["build_playback_speed"] = runpy.run_path(str(source.parent / "queue_controls.py"))["build_playback_speed"]
Runtime = namespace["HomeiiFlowRuntime"]

class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_seek_and_speed_target_owning_queue(self):
        for command, payload, api, arguments in (
            ("seek", {"seek_position": 42}, "player_queues/seek", {"position": 42}),
            ("playback_speed", {"speed": 1.25}, "player_queues/set_playback_speed", {"speed": 1.25}),
        ):
            r = self.runtime()
            r._resolve_ma_player_id = lambda _: "native-child"
            r._bump_snapshot_revision = Mock()
            r.async_music_assistant_command = AsyncMock(side_effect=[{"data": {"queue_id": "leader", "current_item": {"media_item": {"media_type": "podcast_episode"}}}}, {"data": None}])
            await r.async_player_command({"player": "media_player.child", "command": command, **payload})
            self.assertEqual(r.async_music_assistant_command.call_args.args[0], {"command": api, "args": {"queue_id": "leader", **arguments}})

    def test_speed_rejects_invalid_values(self):
        for value in (None, True, float("nan"), float("inf"), 0, 3.1, "bad"):
            with self.assertRaises(ValueError):
                namespace["build_playback_speed"]("queue", {"speed": value})

    async def test_music_speed_is_rejected_before_sending_mutation(self):
        r = self.runtime()
        r._resolve_ma_player_id = lambda value: value
        r.async_music_assistant_command = AsyncMock(return_value={"data": {"queue_id": "computer", "current_item": {"media_item": {"media_type": "track"}}}})
        with self.assertRaisesRegex(ValueError, "podcast episodes and audiobooks"):
            await r.async_player_command({"player": "computer", "command": "playback_speed", "speed": 1.25})
        self.assertEqual(r.async_music_assistant_command.await_count, 1)

    def runtime(self):
        r = Runtime()
        r.hass = SimpleNamespace(services=SimpleNamespace(has_service=lambda *args: True))
        r._announcement_targets = lambda p: p["players"]
        r._preferred_announcement_say_service = lambda _: "google_translate_say"
        r.async_record_announcement = AsyncMock(return_value={"announcement": {}})
        r.async_record_activity = AsyncMock()
        r.async_call_service_response = AsyncMock()
        return r

    async def test_partial_is_not_full_success(self):
        r = self.runtime()
        r.async_call_service_response.side_effect = [None, RuntimeError("offline")]
        result = await r.async_send_announcement({"message": "https://example.test/test.mp3", "players": ["a", "b"]})
        self.assertFalse(result["ok"])
        self.assertTrue(result["sent"])
        self.assertTrue(result["partial"])

    async def test_explicit_tts_failure_is_not_replayed(self):
        r = self.runtime()
        r.async_call_service_response.side_effect = asyncio.TimeoutError("uncertain")
        with self.assertRaises(RuntimeError):
            await r.async_send_announcement({"message": "test", "players": ["a"], "tts_entity": "tts.test"})
        self.assertEqual(r.async_call_service_response.await_count, 1)

    async def test_tts_language_is_top_level(self):
        r = self.runtime()
        result = await r.async_send_announcement({"message": "test", "players": ["a"], "tts_entity": "tts.test", "language": "he", "volume": 20})
        data = r.async_call_service_response.call_args.args[2]
        self.assertEqual(namespace["tts"].generate_media_source_id.call_args.kwargs["language"], "he")
        self.assertTrue(data["announce"] )
        self.assertEqual(data["extra"], {"announce_volume": 20})
        self.assertNotIn("options", data)
        self.assertTrue(result["ok"])

    async def test_transfer_between_members_of_same_queue_is_noop(self):
        r = self.runtime()
        r._resolve_ma_player_id = lambda v: v
        r.async_music_assistant_command = AsyncMock(return_value={"data": {"queue_id": "leader"}})
        result = await r.async_transfer_queue({"source_player": "leader", "target_player": "child"})
        self.assertTrue(result["noop"])
        self.assertEqual(r.async_music_assistant_command.await_count, 2)

    async def test_queue_switches_resolve_group_owner_and_preserve_false(self):
        for command, field in (("crossfade", "crossfade_enabled"), ("autoplay", "autoplay_enabled")):
            r = self.runtime()
            r._resolve_ma_player_id = lambda _: "native-child"
            r._bump_snapshot_revision = Mock()
            r.async_music_assistant_command = AsyncMock(side_effect=[{"data": {"queue_id": "native-leader"}}, {"data": None}])
            result = await r.async_player_command({"player": "media_player.child", "command": command, field: False})
            self.assertTrue(result["ok"])
            self.assertEqual(r.async_music_assistant_command.call_args.args[0], {"command": f"player_queues/{command}", "args": {"queue_id": "native-leader", field: False}})

    def test_queue_switch_rejects_string_boolean_and_missing_queue(self):
        build = namespace["build_queue_switch"]
        with self.assertRaises(ValueError):
            build("crossfade", "queue", {"crossfade_enabled": "false"})
        with self.assertRaises(ValueError):
            build("autoplay", "", {"autoplay_enabled": True})

    async def test_unjoin_uses_native_ma_id(self):
        r = self.runtime()
        r._resolve_ma_player_id = lambda _: "native-child"
        r._bump_snapshot_revision = Mock()
        r.async_music_assistant_command = AsyncMock()
        await r.async_player_command({"player": "media_player.child", "command": "unjoin"})
        r.async_music_assistant_command.assert_awaited_once_with({"command": "players/cmd/ungroup", "args": {"player_id": "native-child"}})

    async def test_group_changes_use_native_ids_and_one_ma_command(self):
        r = self.runtime()
        r.hass.bus = SimpleNamespace(async_fire=Mock())
        r._resolve_ma_player_id = lambda value: value.replace("media_player.", "native-")
        r._bump_snapshot_revision = Mock()
        r.async_music_assistant_command = AsyncMock()
        await r.async_apply_group({"owner": "media_player.leader", "members": ["media_player.leader", "media_player.new"], "remove_members": ["media_player.old"]})
        r.async_music_assistant_command.assert_awaited_once_with({"command": "players/cmd/set_members", "args": {"target_player": "native-leader", "player_ids_to_add": ["native-new"], "player_ids_to_remove": ["native-old"]}})

if __name__ == "__main__":
    unittest.main()
class TimerExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_and_completed_catchup_execute_only_once(self):
        r = Runtime()
        r.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        r._timer_execution_tasks = {}
        r.async_call_service_response = AsyncMock()
        r.async_record_activity = AsyncMock()
        timer = {"id": "sleep", "ends_at": "first", "player": "media_player.test", "action": "pause"}
        first, second = await asyncio.gather(r.async_execute_timer(timer), r.async_execute_timer(timer))
        await r.async_execute_timer(timer)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(r.async_call_service_response.await_count, 1)
        self.assertEqual(r.async_record_activity.await_count, 1)
        await r.async_execute_timer({**timer, "ends_at": "next"})
        self.assertEqual(r.async_call_service_response.await_count, 2)

    async def test_uncertain_timer_failure_is_not_replayed(self):
        r = Runtime()
        r.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        r._timer_execution_tasks = {}
        r.async_call_service_response = AsyncMock(side_effect=TimeoutError("uncertain"))
        r.async_record_activity = AsyncMock()
        timer = {"id": "sleep", "ends_at": "first", "player": "media_player.test"}
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                await r.async_execute_timer(timer)
        self.assertEqual(r.async_call_service_response.await_count, 1)

class ScheduleExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_scheduled_callbacks_claim_run_before_dispatch(self):
        from datetime import datetime, UTC
        parsed = ast.parse(source.read_text(encoding="utf-8"))
        runner = next(n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiScheduleRunner")
        runner.body = [n for n in runner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_fire"]
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), runner], type_ignores=[])
        now = datetime.now(UTC)
        ns = {**namespace, "_local_datetime": lambda value=None: value or now, "_homeii_weekday": lambda value: 1}
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), ns)
        r = ns["HomeiiScheduleRunner"]()
        r.schedule = {"id": "daily", "profile_id": "default"}
        r.schedule_id = "daily"; r.profile_id = "default"; r.key = "default:daily"
        r.runtime = SimpleNamespace(_last_schedule_runs={}, _schedule_run_key=lambda *args: "due", schedules=lambda: [r.schedule], _schedule_unsubs={}, hass=object())
        r.manager = SimpleNamespace(reschedule_all=Mock(), write_status=Mock())
        started = asyncio.Event(); release = asyncio.Event()
        async def dispatch(*args, **kwargs):
            started.set()
            await release.wait()
            return {"ok": True}
        r.action_queue = SimpleNamespace(async_run=AsyncMock(side_effect=dispatch))
        first = asyncio.create_task(r.async_fire(now, trigger="timer"))
        await started.wait()
        second = await r.async_fire(now, trigger="switch")
        self.assertTrue(second["skipped"])
        release.set()
        self.assertTrue((await first)["ok"])
        self.assertEqual(r.action_queue.async_run.await_count, 1)

    def test_schedule_switch_uses_shared_runner(self):
        text = (source.parent / "switch.py").read_text(encoding="utf-8")
        parsed = ast.parse(text)
        switch = next(n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowScheduleSwitch")
        fire = next(n for n in switch.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_fire")
        calls = [n.func.attr for n in ast.walk(fire) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertIn("async_run_scheduled_schedule", calls)
        self.assertNotIn("async_execute_schedule", calls)

class QueueMoveTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_uses_ma_front_of_upcoming_contract(self):
        r = Runtime()
        r._resolve_ma_player_id = lambda player: player
        r.async_music_assistant_command = AsyncMock(return_value={})
        r._bump_snapshot_revision = Mock()
        for shift in (None, 0, 8, -8):
            result = await r.async_queue_action({"player": "owner", "queue_id": "queue", "action": "next", "queue_item_id": "item", "position_shift": shift})
            self.assertTrue(result["ok"])
            self.assertEqual(r.async_music_assistant_command.call_args.args[0]["args"]["pos_shift"], 0)

    async def test_move_to_requires_a_real_shift_instead_of_false_success(self):
        r = Runtime()
        r._resolve_ma_player_id = lambda player: player
        r.async_music_assistant_command = AsyncMock()
        with self.assertRaises(ValueError):
            await r.async_queue_action({"player": "owner", "queue_id": "queue", "action": "move_to", "queue_item_id": "item"})
        r.async_music_assistant_command.assert_not_awaited()
