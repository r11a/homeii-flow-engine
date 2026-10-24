import ast
from pathlib import Path
from datetime import datetime, UTC, timedelta
from types import SimpleNamespace
import unittest

class OverrideTests(unittest.TestCase):
 def test_pause_expires_without_changing_enabled_or_schedule(self):
  path=Path(__file__).resolve().parents[1]/'custom_components/homeii_flow/runtime.py'
  cls=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef) and n.name=='HomeiiFlowRuntime')
  method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_volume_rule_active')
  ns={'Any':object,'datetime':datetime,'UTC':UTC,'_safe_list':lambda x:x or [],'_homeii_weekday':lambda x:0,'_time_window_active':lambda *args:True,'_parse_utc_datetime':lambda x:datetime.fromisoformat(x) if x else None}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),str(path),'exec'),ns)
  now=datetime.now(UTC);rule={'enabled':True,'suspended_until':(now+timedelta(minutes=30)).isoformat()}
  active=ns['_volume_rule_active']
  self.assertFalse(active(SimpleNamespace(),rule,now))
  self.assertTrue(active(SimpleNamespace(),rule,now+timedelta(minutes=31)))
  rule['enabled']=False
  self.assertFalse(active(SimpleNamespace(),rule,now+timedelta(minutes=31)))
