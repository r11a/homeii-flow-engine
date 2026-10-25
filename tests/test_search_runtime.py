"""Search empty responses must not hide late MA provider results."""
from __future__ import annotations

import ast
import time
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/homeii_flow/runtime.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
method = next(node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == 'HomeiiFlowRuntime'
              for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'async_get_search')
namespace = dict(time=time, Any=object, DEFAULT_PROFILE_ID='default', _safe_list=lambda value: value if isinstance(value, list) else [], _clean_string=lambda value: str(value or '').strip())
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])), str(SOURCE), 'exec'), namespace)


class SearchRuntimeTests(IsolatedAsyncioTestCase):
    async def test_empty_provider_result_does_not_mask_the_next_response(self):
        class Runtime:
            async_get_search = namespace['async_get_search']
        runtime = Runtime()
        runtime._search_cache = {}
        runtime._music_assistant_provider_ids = AsyncMock(return_value=['spotify-test'])
        runtime.async_music_assistant_command = AsyncMock(side_effect=[{'data': []}, {'data': [{'name': 'אביתר בנאי'}]}])
        runtime.normalize_search_response = lambda data, **kwargs: {'data': data, 'groups': {}, 'items': data}
        payload = {'query': 'אביתר בנאי', 'provider_only': True}
        self.assertEqual((await runtime.async_get_search(payload))['items'], [])
        self.assertEqual((await runtime.async_get_search(payload))['items'][0]['name'], 'אביתר בנאי')
        self.assertEqual(runtime.async_music_assistant_command.await_count, 2)
        self.assertEqual(runtime.async_music_assistant_command.call_args.args[0]['args']['search_query'], 'אביתר בנאי')
        await runtime.async_get_search(payload)
        self.assertEqual(runtime.async_music_assistant_command.await_count, 2)
