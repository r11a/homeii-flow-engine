"""Real artwork lighting controller with isolated Home Assistant boundaries."""
import ast
import asyncio
import copy
from datetime import timedelta, datetime, UTC
from io import BytesIO
import logging
from pathlib import Path
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from PIL import Image

source = Path(__file__).resolve().parents[1] / 'custom_components/homeii_flow/artwork_lighting.py'
tree = ast.parse(source.read_text(encoding='utf-8-sig'))
nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
namespace = dict(datetime=datetime, UTC=UTC, asyncio=asyncio, copy=copy, timedelta=timedelta, BytesIO=BytesIO, time=time, Image=Image, _LOGGER=logging.getLogger(__name__), DATA_COMPONENT='media_player', callback=lambda fn:fn, async_track_state_change_event=lambda *args:lambda:None)
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(source),'exec'), namespace)
Controller = namespace['ArtworkLighting']

def picture(color):
    stream=BytesIO(); Image.new('RGB',(24,24),color).save(stream,format='PNG'); return stream.getvalue()

class ArtworkLightingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.player=SimpleNamespace(state='playing',attributes={'media_title':'Song','entity_picture':'cover','volume_level':.5})
        self.light=SimpleNamespace(state='on',attributes={'supported_color_modes':['rgb'],'supported_features':32})
        self.states={'media_player.computer':self.player,'media_player.kitchen':self.player,'light.desk':self.light}
        self.image=AsyncMock(return_value=(picture((200,40,20)),'image/png'))
        async def executor(fn,*args): return fn(*args)
        self.hass=SimpleNamespace(states=SimpleNamespace(get=self.states.get),data={'media_player':SimpleNamespace(get_entity=lambda _:SimpleNamespace(async_get_media_image=self.image))},services=SimpleNamespace(async_call=AsyncMock()),async_add_executor_job=executor)
        self.runtime=SimpleNamespace(hass=self.hass,_storage={'artwork_lighting':{}},async_save=AsyncMock())
        self.controller=Controller(self.runtime)
        self.controller._schedule=lambda _:None

    async def configure(self):
        await self.controller.configure({'player':'media_player.computer','lights':['light.desk'],'enabled':True})

    async def test_persists_and_follows_without_a_card(self):
        await self.configure()
        await self.controller._apply('media_player.computer')
        self.runtime.async_save.assert_awaited_once()
        args=self.hass.services.async_call.call_args.args
        self.assertEqual(args[:2],('light','turn_on'))
        self.assertEqual(args[2]['rgb_color'],[200,40,20])
        self.assertEqual(args[2]['brightness_pct'],18)
        await self.controller._apply('media_player.computer')
        self.hass.services.async_call.assert_awaited_once()
        restored=Controller(self.runtime)
        self.assertTrue(restored.snapshot()['rules']['media_player.computer']['enabled'])

    async def test_disable_during_image_download_never_sends_light_command(self):
        await self.configure()
        async def image():
            await self.controller.configure({'player':'media_player.computer','enabled':False})
            return picture((20,30,200)),'image/png'
        self.image.side_effect=image
        await self.controller._apply('media_player.computer')
        self.hass.services.async_call.assert_not_awaited()

    async def test_track_change_during_download_does_not_apply_old_colors(self):
        await self.configure()
        async def image():
            self.player.attributes['media_title']='New song'
            return picture((20,30,200)),'image/png'
        self.image.side_effect=image
        await self.controller._apply('media_player.computer')
        self.hass.services.async_call.assert_not_awaited()

    async def test_conflicting_players_rejected_and_unavailable_lights_reported(self):
        await self.configure()
        with self.assertRaises(ValueError):
            await self.controller.configure({'player':'media_player.kitchen','lights':['light.desk'],'enabled':True})
        self.light.state='unavailable'
        await self.controller._apply('media_player.computer')
        self.hass.services.async_call.assert_not_awaited()
        self.assertEqual(self.controller.status['media_player.computer']['state'],'partial')

    async def test_storage_failure_rolls_back(self):
        self.runtime.async_save.side_effect=RuntimeError('disk')
        with self.assertRaises(RuntimeError): await self.configure()
        self.assertEqual(self.controller.snapshot()['rules'],{})

    async def test_paused_player_does_not_turn_on_lights(self):
        await self.configure(); self.player.state='paused'
        await self.controller._apply('media_player.computer')
        self.hass.services.async_call.assert_not_awaited()

    async def test_new_track_color_follows_without_any_frontend(self):
        await self.configure()
        await self.controller._apply('media_player.computer')
        self.player.attributes['media_title']='Second song'
        self.image.return_value=(picture((20,40,200)),'image/png')
        self.controller._attempt.clear()
        await self.controller._apply('media_player.computer')
        self.assertEqual(self.hass.services.async_call.call_count,2)
        self.assertEqual(self.hass.services.async_call.call_args.args[2]['rgb_color'],[20,40,200])
        self.assertEqual(self.controller.status['media_player.computer']['media_title'],'Second song')
        self.assertTrue(self.controller.status['media_player.computer']['updated_at'])
