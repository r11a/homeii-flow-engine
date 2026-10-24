import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
p=Path(__file__).resolve().parents[1]/'custom_components/homeii_flow/queue_copy.py'
spec=importlib.util.spec_from_file_location('queue_copy',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class CopyTests(IsolatedAsyncioTestCase):
 async def test_copies_without_mutating_source(self):
  queue={'items':2,'current_index':1}
  items=[{'media_item':{'uri':'library://track/1'}},{'media_item':{'uri':'library://track/2'}}]
  r=SimpleNamespace(async_music_assistant_command=AsyncMock(side_effect=[{'data':items},{'data':queue},{}]))
  self.assertTrue((await m.copy_queue(r,'source','target',queue))['copied'])
  mutation=r.async_music_assistant_command.await_args_list[-1].args[0]
  self.assertEqual(mutation['command'],'player_queues/play_media');self.assertEqual(mutation['args']['queue_id'],'target');self.assertEqual(mutation['args']['start_item'],'library://track/2')
  self.assertEqual(r.async_music_assistant_command.await_count,3)
 async def test_rejects_same_group_dynamic_and_incomplete_snapshots(self):
  r=SimpleNamespace(async_music_assistant_command=AsyncMock(return_value={'data':[]}))
  for source,target,queue in [('a','a',{'items':2}),('a','b',{'items':2,'is_dynamic':True}),('a','b',{'items':2})]:
   with self.assertRaises(ValueError):await m.copy_queue(r,source,target,queue)
  self.assertTrue(all(c.args[0]['command']=='player_queues/items' for c in r.async_music_assistant_command.await_args_list))
