import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

p=Path(__file__).resolve().parents[1]/'custom_components/homeii_flow/volume_transitions.py'
spec=importlib.util.spec_from_file_location('fades',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class FadeTests(IsolatedAsyncioTestCase):
 def runtime(self,state='playing',volume=0):
  return SimpleNamespace(hass=SimpleNamespace(states=SimpleNamespace(get=lambda _:SimpleNamespace(state=state,attributes={'volume_level':volume}))),active_volume_rule_summaries=lambda:[{'player':'speaker','max_volume':30}],async_call_service_response=AsyncMock(),async_record_activity=AsyncMock())
 async def test_fade_respects_limit(self):
  r=self.runtime()
  with patch.object(m.asyncio,'sleep',new=AsyncMock()),patch.object(m.time,'monotonic',side_effect=[0,5,10]):
   await m.run_transition(r,'speaker',80,10)
  self.assertEqual([c.args[2]['volume_level'] for c in r.async_call_service_response.await_args_list],[.3,.3])
 async def test_manual_change_or_pause_stops_fade(self):
  for r in [self.runtime(volume=.75),self.runtime(state='paused')]:
   with patch.object(m.asyncio,'sleep',new=AsyncMock()):await m.run_transition(r,'speaker',80,10)
   r.async_call_service_response.assert_not_awaited()
