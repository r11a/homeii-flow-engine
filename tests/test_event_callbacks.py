"""Verify real runtime callbacks execute in the HA event loop, not its executor."""
import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock

SOURCE=Path(__file__).resolve().parents[1]/'custom_components/homeii_flow/runtime.py'
tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
methods={'async_start_orchestration','_schedule_background_tick','_schedule_media_cache_save','_schedule_media_cache_warm'}
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='HomeiiFlowRuntime')
cls.body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in methods]
registered=[]
def callback(func):
    func.ha_callback=True
    return func
def register(hass, *args, **kwargs):
    registered.append(next(arg for arg in args if callable(arg)))
    return lambda:None
ns=dict(callback=callback,asyncio=asyncio,datetime=datetime,timedelta=timedelta,_local_datetime=lambda:datetime.now(UTC),async_call_later=register,async_track_time_interval=register,async_track_time_change=register)
exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(SOURCE),'exec'),ns)

class CallbackTests(IsolatedAsyncioTestCase):
    def setUp(self):
        registered.clear()
        self.tasks=[]
        def create_task(coro):
            try: loop=asyncio.get_running_loop()
            except RuntimeError:
                coro.close()
                raise
            task=loop.create_task(coro)
            self.tasks.append(task)
            return task
        self.runtime=ns['HomeiiFlowRuntime']()
        self.runtime.hass=SimpleNamespace(async_create_task=create_task)
        self.runtime._orchestration_unsub=None
        self.runtime._schedule_manager=SimpleNamespace(start=lambda:None)
        self.runtime._delayed_tick_unsubs=[]
        self.runtime.async_tick_orchestration=AsyncMock()
    async def dispatch(self):
        for fn in list(registered):
            if getattr(fn,'ha_callback',False): fn(datetime.now(UTC))
            else: await asyncio.to_thread(fn,datetime.now(UTC))
        await asyncio.gather(*self.tasks)
    async def test_interval_minute_and_delayed_ticks_run_on_loop(self):
        self.runtime.async_start_orchestration()
        await self.dispatch()
        self.assertEqual(self.runtime.async_tick_orchestration.await_count,5)
        self.assertEqual({c.kwargs['trigger'] for c in self.runtime.async_tick_orchestration.await_args_list},{'startup','interval','minute','startup_delayed','startup_probe'})
    async def test_cache_save_and_warm_are_not_dispatched_to_executor(self):
        self.runtime._media_cache_save_unsub=None
        self.runtime._media_cache_warm_unsub=None
        self.runtime._media_cache_warm_task=None
        self.runtime._async_save_media_cache=AsyncMock()
        self.runtime._async_warm_media_cache=AsyncMock()
        self.runtime._schedule_media_cache_save()
        self.runtime._schedule_media_cache_warm()
        await self.dispatch()
        self.runtime._async_save_media_cache.assert_awaited_once()
        self.runtime._async_warm_media_cache.assert_awaited_once()

if __name__=='__main__':main()