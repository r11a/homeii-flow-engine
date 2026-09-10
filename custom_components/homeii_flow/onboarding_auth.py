"""One-shot MA login used only during explicit Engine onboarding."""
import asyncio
from urllib.parse import urlsplit, urlunsplit

from .const import MUSIC_ASSISTANT_SCHEMA_MIN


def is_ha_interface_url(url):
    """Recognize HA UI/ingress paths, without guessing from a custom port alone."""
    parsed = urlsplit(url.strip())
    route = (parsed.path + '/' + parsed.fragment).lower()
    return any(part in route for part in (
        '/api/hassio_ingress/', '/hassio/ingress/', '/dashboard-',
        '/lovelace', '/config/', '/app/', '_music_assistant',
    ))


async def create_onboarding_token(session, url, username, password):
    """Exchange MA built-in credentials for a dedicated token; never retain credentials."""
    if is_ha_interface_url(url):
        raise ValueError("ma_ingress_url")
    parts = urlsplit(url.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('invalid_url')
    endpoint = urlunsplit(('wss' if parts.scheme == 'https' else 'ws', parts.netloc, parts.path.rstrip('/') + '/ws', '', ''))
    async with asyncio.timeout(25):
        async with session.ws_connect(endpoint, heartbeat=20) as ws:
            hello = await ws.receive_json()
            if not isinstance(hello, dict) or int(hello.get('schema_version') or hello.get('api_schema_version') or 0) < MUSIC_ASSISTANT_SCHEMA_MIN:
                raise ValueError('unsupported_ma_version')
            counter = 0
            async def command(name, args):
                nonlocal counter
                counter += 1
                message_id = f'homeii_setup_{counter}'
                await ws.send_json({'message_id': message_id, 'command': name, 'args': args})
                while True:
                    result = await ws.receive_json()
                    if not isinstance(result, dict) or result.get('message_id') != message_id:
                        continue
                    if result.get('error') or result.get('error_code'):
                        raise ValueError('automatic_login_failed')
                    return result.get('result')
            login = await command('auth/login', {'username': username, 'password': password, 'provider_id': 'builtin', 'device_name': 'HOMEii Flow Engine setup'})
            if not isinstance(login, dict) or not login.get('success') or not isinstance(login.get('access_token'), str):
                raise ValueError('automatic_login_failed')
            await command('auth', {'token': login['access_token'], 'device_name': 'HOMEii Flow Engine setup'})
            token = await command('auth/token/create', {'name': 'HOMEii Flow Engine'})
            if not isinstance(token, str) or not token.strip():
                raise ValueError('automatic_login_failed')
            return token
