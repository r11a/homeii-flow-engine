"""Onboarding credential exchange contract without a live MA account."""
import ast
import asyncio
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from unittest import IsolatedAsyncioTestCase

source = Path(__file__).resolve().parents[1] / 'custom_components/homeii_flow/onboarding_auth.py'
nodes = [n for n in ast.parse(source.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
ns = dict(asyncio=asyncio, urlsplit=urlsplit, urlunsplit=urlunsplit, MUSIC_ASSISTANT_SCHEMA_MIN=63)
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), ns)

class Socket:
    def __init__(self, denied=False): self.sent=[]; self.denied=denied
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def send_json(self,data): self.sent.append(data)
    async def receive_json(self):
        if not self.sent: return {'schema_version':63}
        req=self.sent[-1]
        result={'auth/login':{'success':not self.denied,'access_token':'short'},'auth':{},'auth/token/create':'dedicated'}[req['command']]
        return {'message_id':req['message_id'],'result':result}
class Session:
    def __init__(self,denied=False): self.ws=Socket(denied)
    def ws_connect(self,url,**kw): self.url=url; return self.ws
class OnboardingTests(IsolatedAsyncioTestCase):
    async def test_exchange_uses_dedicated_token(self):
        session=Session()
        token=await ns['create_onboarding_token'](session,'http://ma:8095','user','secret')
        self.assertEqual(token,'dedicated')
        self.assertEqual(session.url,'ws://ma:8095/ws')
        self.assertEqual([x['command'] for x in session.ws.sent],['auth/login','auth','auth/token/create'])
        self.assertNotIn('password',session.ws.sent[-1]['args'])
    async def test_failed_login_does_not_create_token(self):
        session=Session(True)
        with self.assertRaisesRegex(ValueError,'automatic_login_failed'):
            await ns['create_onboarding_token'](session,'http://ma:8095','user','bad')
        self.assertEqual(len(session.ws.sent),1)
    async def test_rejects_credentials_and_fragments_in_url(self):
        for url in ['http://user:pass@ma:8095','http://ma:8095/#/home','file:///tmp']:
            with self.assertRaisesRegex(ValueError,'invalid_url'):
                await ns['create_onboarding_token'](Session(),url,'user','secret')

    async def test_ingress_has_specific_error(self):
        for url in ['http://ha:8123/api/hassio_ingress/abc/', 'http://ha:8123/d5369777_music_assistant_beta#/', 'http://ha:8123/dashboard-clean/ma']:
            with self.assertRaisesRegex(ValueError,'ma_ingress_url'):
                await ns['create_onboarding_token'](Session(),url,'user','secret')
