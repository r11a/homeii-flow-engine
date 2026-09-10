import ast
import copy
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

source=Path(__file__).resolve().parents[1]/"custom_components/homeii_flow/interface_preferences.py"
tree=ast.parse(source.read_text(encoding="utf-8"))
ns={"copy":copy,"re":re,"DEFAULT_PROFILE_ID":"default"}
exec(compile(ast.Module(body=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))],type_ignores=[]),str(source),"exec"),ns)

class InterfacePreferencesTests(IsolatedAsyncioTestCase):
    async def test_profile_isolation_partial_updates_and_persistence(self):
        runtime=SimpleNamespace(_storage={},async_save=AsyncMock())
        await ns["save_preferences"](runtime,{"profile_id":"tablet","night_mode":"auto","night_start":"22:30","night_days":[0,1]})
        await ns["save_preferences"](runtime,{"profile_id":"tablet","night_end":"06:00"})
        self.assertEqual(ns["read_preferences"](runtime,"tablet")["night_start"],"22:30")
        self.assertEqual(ns["read_preferences"](runtime,"phone"),{})
        self.assertEqual(runtime.async_save.await_count,2)
    async def test_validation_and_failed_save_never_replace_working_settings(self):
        runtime=SimpleNamespace(_storage={},async_save=AsyncMock())
        await ns["save_preferences"](runtime,{"night_mode":"off"})
        for payload in ({"night_mode":"bad"},{"night_start":"25:00"},{"night_days":[True]},{"night_days":[7]}):
            with self.assertRaises(ValueError):await ns["save_preferences"](runtime,payload)
        runtime.async_save.side_effect=RuntimeError("disk")
        with self.assertRaises(RuntimeError):await ns["save_preferences"](runtime,{"night_mode":"on"})
        self.assertEqual(ns["read_preferences"](runtime),{"night_mode":"off"})

    async def test_wheel_user_isolation_and_global_permissions(self):
        runtime=SimpleNamespace(_storage={},async_save=AsyncMock())
        data={"scope":"user","context":"main","preference":{"hidden":["queue"],"order":["play","queue"]}}
        await ns["save_wheel_preferences"](runtime,data,"alice")
        self.assertEqual(ns["read_wheel_preferences"](runtime,None,"bob")["user"],{})
        self.assertEqual(ns["read_wheel_preferences"](runtime,None,"alice")["user"]["main"]["hidden"],["queue"])
        with self.assertRaises(ValueError):
            await ns["save_wheel_preferences"](runtime,{**data,"scope":"global"},"alice")
        await ns["save_wheel_preferences"](runtime,{**data,"scope":"global"},"admin",True)
        self.assertIn("main",ns["read_wheel_preferences"](runtime,None,"bob")["global"])
        before=copy.deepcopy(runtime._storage)
        runtime.async_save.side_effect=RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await ns["save_wheel_preferences"](runtime,{**data,"context":"queue"},"alice")
        self.assertEqual(runtime._storage,before)
