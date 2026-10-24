import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
p=Path(__file__).resolve().parents[1]/"custom_components/homeii_flow/instance_registry.py"
spec=importlib.util.spec_from_file_location("instances",p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class InstanceTests(unittest.TestCase):
 def test_configured_server_never_uses_another_discovered_server(self):
  import ast
  source=p.parent/'runtime.py'
  cls=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef) and n.name=='HomeiiFlowRuntime')
  methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('music_assistant_base_urls','music_assistant_config_entries_snapshot')]
  ns={'Any':object,'_normalized_http_url':lambda value:value}
  exec(compile(ast.fix_missing_locations(ast.Module(body=methods,type_ignores=[])),str(source),'exec'),ns)
  r=SimpleNamespace(_entries={'a':SimpleNamespace(music_assistant_url='http://own:8095',music_assistant_external_url='')},hass=SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _:[])))
  self.assertEqual(ns['music_assistant_base_urls'](r),['http://own:8095'])
  self.assertEqual(ns['music_assistant_config_entries_snapshot'](r),[])
 def test_explicit_identity_never_falls_back(self):
  first=SimpleNamespace(entries=[SimpleNamespace(instance_id="parent")]);second=SimpleNamespace(entries=[SimpleNamespace(instance_id="child")])
  data={"runtime":first,"instance_runtimes":{"a":first,"b":second}}
  self.assertIs(m.resolve_runtime(data,"child"),second)
  self.assertIs(m.resolve_runtime(data),first)
  with self.assertRaises(ValueError):m.resolve_runtime(data,"missing")

 def test_default_uses_loaded_entry_after_primary_unloads(self):
  primary=SimpleNamespace(entries=[])
  child=SimpleNamespace(entries=[SimpleNamespace(instance_id="child")])
  self.assertIs(m.resolve_runtime({"runtime":primary,"instance_runtimes":{"child":child}}),child)

 def test_artwork_tokens_are_isolated_for_identical_sources(self):
  import ast, hashlib, time
  source=p.parent/'runtime.py'
  cls=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef) and n.name=='HomeiiFlowRuntime')
  methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('register_artwork_source','resolve_artwork_source')]
  ns={'hashlib':hashlib,'time':time,'Any':object}
  exec(compile(ast.fix_missing_locations(ast.Module(body=methods,type_ignores=[])),str(source),'exec'),ns)
  a=SimpleNamespace(_storage_namespace='',_artwork_source_tokens={},_artwork_sources={})
  b=SimpleNamespace(_storage_namespace='_child',_artwork_source_tokens={},_artwork_sources={})
  first=ns['register_artwork_source'](a,'http://ma/image/1')
  second=ns['register_artwork_source'](b,'http://ma/image/1')
  self.assertNotEqual(first,second)
  self.assertEqual(ns['resolve_artwork_source'](a,second.rsplit('/',1)[1]),'')

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
 async def test_secondary_load_reload_and_unload_preserve_primary(self):
  import ast
  from unittest.mock import AsyncMock, Mock
  source=p.parent/'__init__.py'
  tree=ast.parse(source.read_text(encoding='utf-8'))
  functions=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('async_setup_entry','async_unload_entry','async_get_runtime')]
  class Runtime:
   def __init__(self,hass,storage_namespace=''):
    self._storage={};self.namespace=storage_namespace;self.entries=[]
    self.async_save=AsyncMock();self.async_load=AsyncMock()
    self.async_start_orchestration=Mock();self.async_stop_orchestration=Mock()
    self.artwork_lighting=SimpleNamespace(start=Mock(),stop=Mock())
   def register_entry(self,key,**kw):self.entries=[SimpleNamespace(entry_id=key,**kw)]
   def unregister_entry(self,key):self.entries=[]
  def entry(key):return SimpleNamespace(entry_id=key,data={'instance_id':key},options={},title=key,async_on_unload=Mock(),add_update_listener=Mock())
  a=entry('a');b=entry('b');primary=Runtime(None)
  hass=SimpleNamespace(data={'homeii_flow':{'runtime':primary}},config_entries=SimpleNamespace(async_entries=lambda _: [a,b],async_forward_entry_setups=AsyncMock(),async_unload_platforms=AsyncMock(return_value=True)))
  ns={'HomeiiFlowRuntime':Runtime,'resolve_runtime':m.resolve_runtime,'async_prepare_runtime':AsyncMock(return_value=primary),'DOMAIN':'homeii_flow','PLATFORMS':[], '_async_update_entry':Mock()}
  for name in ('INSTANCE_ID','PROFILE_ID','ENABLE_EXPERIMENTAL','MUSIC_ASSISTANT_URL','MUSIC_ASSISTANT_EXTERNAL_URL','MUSIC_ASSISTANT_TOKEN'):ns['CONF_'+name]=name.lower()
  ns.update(DEFAULT_INSTANCE_ID='default',DEFAULT_PROFILE_ID='default')
  future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
  exec(compile(ast.fix_missing_locations(ast.Module(body=[future,*functions],type_ignores=[])),str(source),'exec'),ns)
  # Out-of-order HA setup must not give the second entry legacy credentials/storage.
  await ns['async_setup_entry'](hass,b)
  await ns['async_setup_entry'](hass,a)
  child=hass.data['homeii_flow']['instance_runtimes']['b']
  self.assertEqual(child.namespace,'_b');self.assertIsNot(child,primary)
  await ns['async_unload_entry'](hass,b)
  self.assertEqual(primary.entries[0].instance_id,'a')
  primary.async_stop_orchestration.assert_not_called()
  await ns['async_setup_entry'](hass,b)
  self.assertIs(hass.data['homeii_flow']['instance_runtimes']['b'],child)
  child.async_load.assert_awaited_once()
