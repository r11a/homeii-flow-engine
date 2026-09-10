"""Radio directory artwork must survive required-Engine rendering."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

source=Path(__file__).resolve().parents[1]/'custom_components/homeii_flow/radio_directory.py'
tree=ast.parse(source.read_text(encoding='utf-8'))
nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='station_items']
ns={};exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),ns)

class RadioArtworkTests(TestCase):
    def test_public_station_logo_uses_local_proxy(self):
        runtime=SimpleNamespace(register_artwork_source=lambda source:'/api/homeii_flow/artwork/item/token')
        stations=ns['station_items'](runtime,[{'name':'Radio','url_resolved':'https://radio.test/stream','favicon':'http://radio.test/logo.png','stationuuid':'id'}])
        self.assertEqual(stations[0]['homeii_artwork_url'],'/api/homeii_flow/artwork/item/token')
        self.assertEqual(stations[0]['image'],stations[0]['homeii_artwork_url'])
        self.assertEqual(stations[0]['radio_browser_id'],'id')

    def test_absent_logo_is_not_fabricated_and_invalid_stream_is_dropped(self):
        runtime=SimpleNamespace(register_artwork_source=lambda source:source)
        stations=ns['station_items'](runtime,[{'name':'Radio','url':'https://radio.test/stream','favicon':''},{'url':'javascript:bad'}])
        self.assertEqual(len(stations),1)
        self.assertEqual(stations[0]['image'],'')
