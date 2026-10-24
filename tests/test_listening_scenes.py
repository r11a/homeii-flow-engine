import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

path = Path(__file__).resolve().parents[1] / "custom_components/homeii_flow/listening_scenes.py"
spec = importlib.util.spec_from_file_location("listening_scenes", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class SceneTests(IsolatedAsyncioTestCase):
    def runtime(self):
        return SimpleNamespace(_storage={}, async_save=AsyncMock(),
            _player_readiness=lambda p: {"ready": p != "offline"},
            async_apply_group=AsyncMock(), async_player_command=AsyncMock(),
            async_play_media=AsyncMock(return_value={"ok": True}),
            async_record_activity=AsyncMock(), artwork_lighting=SimpleNamespace(configure=AsyncMock()))

    async def test_persistence_profile_and_failed_write_rollback(self):
        r = self.runtime()
        item = await m.save_scene(r, {"name":"Morning", "player":"media_player.kitchen", "media_id":"library://playlist/1"})
        self.assertEqual(m.list_scenes(r), [item])
        self.assertEqual(m.list_scenes(r, "child"), [])
        r.async_save.side_effect = RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await m.save_scene(r, {**item,"scene_id":item["id"],"name":"Changed"})
        self.assertEqual(m.list_scenes(r), [item])

    async def test_preflight_prevents_partial_changes_for_unavailable_member(self):
        r = self.runtime()
        item = await m.save_scene(r,{"name":"Party","player":"media_player.kitchen","members":["offline"],"media_id":"library://playlist/1"})
        with self.assertRaises(ValueError): await m.run_scene(r,{"scene_id":item["id"]})
        r.async_apply_group.assert_not_awaited()
        r.async_play_media.assert_not_awaited()

    async def test_volume_before_play_and_single_dispatch(self):
        r = self.runtime(); order=[]
        async def volume(p): order.append(("volume",p["volume_level"]))
        async def play(p): order.append(("play",p["media_id"]));return {"ok":True}
        r.async_player_command.side_effect=volume;r.async_play_media.side_effect=play
        item=await m.save_scene(r,{"name":"Morning","player":"media_player.kitchen","volume":1,"media_id":"library://playlist/1"})
        result=await m.run_scene(r,{"scene_id":item["id"]})
        self.assertEqual(order,[("volume",.01),("play","library://playlist/1")])
        self.assertTrue(result["ok"])

    async def test_failure_reports_completed_steps_without_replaying(self):
        r=self.runtime();r.async_play_media.side_effect=TimeoutError("MA timeout")
        item=await m.save_scene(r,{"name":"Morning","player":"media_player.kitchen","volume":20,"media_id":"library://playlist/1"})
        result=await m.run_scene(r,{"scene_id":item["id"]})
        self.assertFalse(result["ok"]);self.assertEqual(result["failed_step"],"playback")
        self.assertEqual(result["completed"],["volume"])
        self.assertEqual(r.async_play_media.await_count,1)

    async def test_rejects_invalid_volume_and_cross_profile_delete(self):
        r=self.runtime()
        with self.assertRaises(ValueError): await m.save_scene(r,{"name":"Bad","player":"x","volume":101,"media_id":"library://playlist/1"})
        item=await m.save_scene(r,{"name":"Morning","player":"x","media_id":"library://playlist/1"})
        self.assertFalse((await m.delete_scene(r,{"scene_id":item["id"],"profile_id":"other"}))["deleted"])
