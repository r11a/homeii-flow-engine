import ast
import copy
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
source=Path(__file__).resolve().parents[1]/"custom_components/homeii_flow/saved_playlists.py"
ns={"copy":copy,"uuid":uuid}
tree=ast.parse(source.read_text(encoding="utf-8"))
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))],type_ignores=[]),str(source),"exec"),ns)
class SavedPlaylistTests(IsolatedAsyncioTestCase):
 async def test_save_profile_and_rollback(self):
  runtime=SimpleNamespace(_storage={},async_save=AsyncMock())
  item=await ns["save_playlist"](runtime,{"name":"Evening","uris":["spotify://track/one"]})
  self.assertEqual(ns["list_playlists"](runtime),[item])
  self.assertEqual(ns["list_playlists"](runtime,"other"),[])
  runtime.async_save.side_effect=RuntimeError("disk")
  with self.assertRaises(RuntimeError): await ns["save_playlist"](runtime,{"name":"Changed","uris":["spotify://track/two"],"playlist_id":item["id"]})
  self.assertEqual(ns["list_playlists"](runtime),[item])
 async def test_play_uses_active_queue_and_one_atomic_media_list(self):
  runtime=SimpleNamespace(_storage={},async_save=AsyncMock(),_player_readiness=lambda _: {"ready":True},_resolve_ma_player_id=lambda _:"native",async_music_assistant_command=AsyncMock(side_effect=[{"data":{"queue_id":"group"}},{"ok":True}]))
  item=await ns["save_playlist"](runtime,{"name":"Mix","uris":["spotify://track/one","spotify://track/two"]})
  await ns["play_playlist"](runtime,{"playlist_id":item["id"],"selected_player":"media_player.computer"})
  runtime.async_music_assistant_command.assert_awaited_with({"command":"player_queues/play_media","args":{"queue_id":"group","media":item["uris"],"option":"replace"}})

 async def test_delete_is_profile_scoped_and_rolls_back_failed_persistence(self):
  runtime=SimpleNamespace(_storage={},async_save=AsyncMock())
  item=await ns["save_playlist"](runtime,{"name":"Mix","uris":["library://track/one"]})
  self.assertEqual(await ns["delete_playlist"](runtime,{"profile_id":"other","playlist_id":item["id"]}),{"deleted":False})
  runtime.async_save.side_effect=RuntimeError("disk")
  with self.assertRaises(RuntimeError): await ns["delete_playlist"](runtime,{"playlist_id":item["id"]})
  self.assertEqual(ns["list_playlists"](runtime),[item])
  runtime.async_save.side_effect=None
  self.assertEqual(await ns["delete_playlist"](runtime,{"playlist_id":item["id"]}),{"deleted":True})
  self.assertEqual(ns["list_playlists"](runtime),[])
