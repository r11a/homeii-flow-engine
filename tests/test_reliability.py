"""Execute real Engine methods with fake MA/HA boundaries, without installing HA.

These tests cover adapter behavior, not a running HA integration or real speakers.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components/homeii_flow/runtime.py"
if not SOURCE.exists():
    SOURCE = Path(__file__).with_name("runtime.py")
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
helpers = {"_dict_first", "_clean_string", "_safe_list", "_maybe_number", "_first_non_empty", "_safe_id_part", "_utc_iso", "_playback_snapshot_changed"}
methods = {"_music_assistant_command_allowed", "cached_stats", "stats", "_ha_entity_for_ma_player", "_normalize_ma_player", "_player_readiness", "async_players_snapshot", "async_play_media", "_try_music_queue_command_bridge", "normalize_queue_response", "_queue_payload_root", "_queue_payload_items", "_queue_payload_expected_count", "_resolve_ma_player_id", "is_music_assistant_player"}
methods.update({"_media_type_command_roots", "_music_library_command_attempts", "_try_music_library_command_bridge", "_library_cache_entry", "_library_response", "async_get_library"})
nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
nodes.extend(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in helpers)
runtime_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HomeiiFlowRuntime")
runtime_class.decorator_list = []
runtime_class.body = [node for node in runtime_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in methods]
nodes.append(runtime_class)
registry = SimpleNamespace(entities={}, async_get=lambda _: None)
namespace = {"time": time, "DEFAULT_PROFILE_ID": "default", "asyncio": asyncio, "copy": copy, "Any": Any, "datetime": datetime, "UTC": UTC,
             "HomeiiFlowServiceUnavailable": RuntimeError, "er": SimpleNamespace(async_get=lambda _: registry)}
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), "exec"), namespace)
exec((ROOT / "custom_components/homeii_flow/player_timing.py").read_text(encoding="utf-8"), namespace)
Runtime = namespace["HomeiiFlowRuntime"]


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_playlist_append_is_allowed_without_opening_playlist_administration(self):
        self.assertTrue(Runtime._music_assistant_command_allowed("music/playlists/add_playlist_tracks"))
        self.assertFalse(Runtime._music_assistant_command_allowed("music/playlists/create_playlist"))
        self.assertFalse(Runtime._music_assistant_command_allowed("music/playlists/remove_playlist_tracks"))
        self.assertFalse(Runtime._music_assistant_command_allowed("config/providers/save"))

    def setUp(self):
        registry.entities = {}
        self.runtime = Runtime()
        self.runtime.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: None))
        self.runtime._ma_players_by_entity = {}
        self.runtime._ma_players_by_id = {}
        self.runtime._ma_http_health = {}
        self.runtime._snapshot_revisions = {"queue": 1}
        self.runtime.media_players_snapshot = lambda **_: []
        self.runtime.normalize_media_item = lambda item, **_: dict(item) if isinstance(item, dict) else None
        self.runtime.decorate_artwork_urls = lambda value: value
        self.runtime._snapshot_meta = lambda *_, **__: {}
        self.runtime._bump_snapshot_revision = Mock()
        self.runtime._queue_item_from_player = lambda _: {"name": "Old stale song"}

    async def test_library_pages_have_distinct_offsets_and_cache_entries(self):
        runtime = self.runtime
        runtime._library_cache = {}
        runtime._library_inflight = {}
        runtime._media_cache_metrics = defaultdict(int)
        runtime._snapshot_revisions = {"library": 1}
        runtime._schedule_media_cache_save = Mock()
        runtime._normalize_library_command_items = lambda data, **_: data
        items = [{"uri": f"library://playlist/{index}"} for index in range(691)]
        async def command(payload):
            args = payload["args"]
            self.assertEqual(payload["command"], "music/playlists/library_items")
            return {"data": items[args["offset"]:args["offset"] + args["limit"]]}
        runtime.async_music_assistant_command = AsyncMock(side_effect=command)
        base = {"media_type": "playlist", "limit": 500, "compact": True, "_singleflight_owner": True}
        first = await runtime.async_get_library(base)
        second = await runtime.async_get_library({**base, "offset": 500})
        self.assertEqual(len(first["items"]), 500)
        self.assertEqual(len(second["items"]), 191)
        self.assertEqual(second["items"][0]["uri"], "library://playlist/500")
        self.assertNotIn("data", second)
        self.assertEqual((await runtime.async_get_library({**base, "offset": 500}))["items"], second["items"])
        self.assertEqual(runtime.async_music_assistant_command.await_count, 2)
        self.assertEqual(len(runtime._library_cache), 2)

    def test_entity_stats_skip_artwork_generation_and_share_snapshot(self):
        self.runtime._stats_cache = None
        self.runtime._stats_cache_at = 0
        self.runtime.media_players_snapshot = Mock(return_value=[])
        self.runtime.services_snapshot = lambda: {}
        first = self.runtime.cached_stats()
        self.assertIs(first, self.runtime.cached_stats())
        self.runtime.media_players_snapshot.assert_called_once_with(include_artwork=False)
        self.runtime._stats_cache = None
        self.runtime.cached_stats()
        self.assertEqual(self.runtime.media_players_snapshot.call_count, 2)

    def test_registry_identity_wins_over_shared_queue(self):
        registry.entities = {"leader": SimpleNamespace(platform="music_assistant", unique_id="leader", entity_id="media_player.leader")}
        self.runtime.media_players_snapshot = lambda **_: [{"entity_id": "media_player.child", "active_queue": "leader"}]
        self.assertEqual(self.runtime._ha_entity_for_ma_player({"player_id": "leader"}), "media_player.leader")
        registry.entities = {}
        self.assertNotEqual(self.runtime._ha_entity_for_ma_player({"player_id": "leader"}), "media_player.child")

    def test_native_player_is_ready_without_ha_entity(self):
        self.runtime._ma_players_by_entity["media_player.native"] = {"available": True, "state": "idle"}
        self.assertTrue(self.runtime._player_readiness("media_player.native")["ready"])
        self.runtime._ma_players_by_entity["media_player.native"]["available"] = False
        self.assertFalse(self.runtime._player_readiness("media_player.native")["ready"])

    def test_one_percent_volume_and_external_source(self):
        player = self.runtime._normalize_ma_player({"player_id": "native", "volume_level": 1, "active_source": "airplay"})
        self.assertEqual(player["volume_level"], 0.01)
        self.assertEqual(player["active_source"], "airplay")
        self.assertEqual(player["active_queue"], "")
        self.assertFalse(player["queue_active"])

    def test_offline_player_is_not_normalized_as_idle(self):
        player = self.runtime._normalize_ma_player({"player_id": "native", "available": False, "playback_state": "idle"})
        self.assertEqual(player["state"], "unavailable")

    def test_schedule_does_not_confirm_unchanged_or_buffering_playback(self):
        changed = namespace["_playback_snapshot_changed"]
        before = {"exists": True, "state": "playing", "media_content_id": "old"}
        self.assertFalse(changed(before, before))
        self.assertFalse(changed(before, {**before, "state": "buffering", "media_content_id": "new"}))
        self.assertTrue(changed(before, {**before, "media_content_id": "new"}))

    async def test_group_members_are_mapped_to_card_entities_leader_first(self):
        self.runtime._ha_entity_for_ma_player = lambda raw: "media_player." + raw["player_id"]
        self.runtime.async_music_assistant_command = AsyncMock(side_effect=[
            {"data": [{"player_id": "leader", "group_members": ["child"], "active_source": "leader"}, {"player_id": "child", "synced_to": "leader", "active_source": "leader"}]},
            {"data": [{"queue_id": "leader"}]},
        ])
        result = await self.runtime.async_players_snapshot()
        for player in result["players"]:
            self.assertEqual(player["attributes"]["group_members"], ["media_player.leader", "media_player.child"])

    async def test_catalog_validates_queue_ownership(self):
        self.runtime.async_music_assistant_command = AsyncMock(side_effect=[
            {"data": [{"player_id": "child", "active_source": "leader"}, {"player_id": "external", "active_source": "airplay"}]},
            {"data": [{"queue_id": "leader"}, {"queue_id": "external"}]},
        ])
        result = await self.runtime.async_players_snapshot()
        self.assertEqual(result["players"][0]["active_queue"], "leader")
        self.assertFalse(result["players"][1]["queue_active"])

    async def queue(self, command):
        self.runtime.async_music_assistant_command = command
        return await self.runtime._try_music_queue_command_bridge(entity_id="native", queue_id="stale-before-grouping", player=None, limit_before=50, limit_after=450)

    async def test_queue_loads_beyond_500_and_uses_current_owner(self):
        items = [{"queue_item_id": str(index), "name": str(index)} for index in range(701)]
        state = {"queue_id": "leader", "items": 701, "current_index": 700}
        async def command(payload):
            if payload["command"] == "player_queues/get_active_queue":
                return {"data": state}
            self.assertEqual(payload["args"]["queue_id"], "leader")
            if payload["command"] == "player_queues/get":
                return {"data": state}
            offset = payload["args"]["offset"]
            return {"data": items[offset:offset + 500]}
        result = (await self.queue(command))[-1]
        self.assertTrue(result["complete"])
        self.assertEqual(result["expected_items"], 701)
        self.assertEqual(result["normalized"]["current_item"]["queue_item_id"], "700")

    async def test_queue_read_error_does_not_become_empty_success(self):
        async def command(payload):
            if payload["command"] == "player_queues/items":
                raise RuntimeError("connection lost")
            return {"data": {"queue_id": "native", "items": 1}}
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            await self.queue(command)

    async def test_same_length_queue_mutation_during_paging_is_rejected(self):
        async def command(payload):
            if payload["command"] == "player_queues/items":
                self.runtime._snapshot_revisions["queue"] += 1
                return {"data": [{"queue_item_id": "new"}]}
            return {"data": {"queue_id": "native", "items": 1}}
        with self.assertRaisesRegex(RuntimeError, "changed while loading"):
            await self.queue(command)

    async def test_empty_queue_does_not_invent_old_player_track(self):
        result = (await self.queue(AsyncMock(return_value={"data": {"queue_id": "native", "items": 0, "current_index": None}})))[-1]
        self.assertEqual(result["normalized"]["items"], [])
        self.assertIsNone(result["normalized"]["current_item"])
        self.assertIsNone(result["normalized"]["current_index"])
        self.assertTrue(result["complete"])

    def test_partial_queue_preserves_current_index_and_explicit_item(self):
        result = self.runtime.normalize_queue_response({"items": [{"name": "first"}], "items_count": 900, "current_index": 700, "current_item": {"name": "playing"}})
        self.assertEqual(result["current_index"], 700)
        self.assertEqual(result["items_count"], 900)
        self.assertEqual(result["current_item"]["name"], "playing")

    async def play(self, enqueue, after):
        self.runtime._ma_players_by_entity["native"] = {"available": True, "state": "idle", "raw_player_id": "native"}
        async def command(payload):
            if payload["command"] == "player_queues/get_active_queue":
                return {"data": {"queue_id": "native", "state": "playing", "current_item": {"queue_item_id": "old"}}}
            if payload["command"] == "player_queues/get":
                return {"data": after}
            return {"data": None}
        self.runtime.async_music_assistant_command = AsyncMock(side_effect=command)
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            return await self.runtime.async_play_media({"player": "native", "media_id": "library://track/new", "enqueue": enqueue, "verify_playback": True})

    async def test_enqueue_does_not_require_playback_change(self):
        result = await self.play("add", {})
        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"])
        self.assertNotIn("player_queues/get", [call.args[0]["command"] for call in self.runtime.async_music_assistant_command.call_args_list])

    async def test_native_playback_verifies_new_queue_item(self):
        result = await self.play("play", {"state": "playing", "current_item": {"queue_item_id": "new"}})
        self.assertTrue(result["verified"])

    async def test_old_playing_track_does_not_confirm_new_request(self):
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            await self.play("play", {"state": "playing", "current_item": {"queue_item_id": "old"}})
        self.runtime._bump_snapshot_revision.assert_called_once()
        mutations = [call for call in self.runtime.async_music_assistant_command.call_args_list if call.args[0]["command"] == "player_queues/play_media"]
        self.assertEqual(len(mutations), 1)


if __name__ == "__main__":
    unittest.main()
