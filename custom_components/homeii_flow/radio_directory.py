"""Radio directory queries with Engine-local artwork URLs."""
from __future__ import annotations
import asyncio
import copy
import time
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession


def station_items(runtime, stations):
    result = []
    for station in stations[:80]:
        if not isinstance(station, dict):
            continue
        uri = station.get('url_resolved') or station.get('url') or ''
        if not isinstance(uri, str) or not uri.startswith(('https://', 'http://')):
            continue
        favicon = station.get('favicon') or ''
        artwork = runtime.register_artwork_source(favicon) if isinstance(favicon,str) and favicon.startswith(('http://','https://')) else ''
        result.append({'uri':uri,'media_type':'radio','name':station.get('name') or station.get('stationuuid') or 'Radio Browser',
                       'homeii_artwork_url':artwork,'image':artwork,'image_url':artwork,
                       'metadata':{'description':' · '.join(str(station.get(key) or '') for key in ('country','tags')).strip(' ·')},
                       'artist_str':station.get('country') or 'Radio Browser','radio_browser_id':station.get('stationuuid') or '',
                       'radio_browser':True,'radio_browser_country':station.get('countrycode') or station.get('country') or ''})
    return result


async def search_stations(runtime, payload):
    query = str(payload.get('query') or '').strip()[:160]
    country = str(payload.get('country') or '').upper()
    tag = str(payload.get('tag') or '').strip()[:80]
    limit = max(8, min(80, int(payload.get('limit') or 40)))
    key = (query,country,tag,limit)
    if not hasattr(runtime,'_radio_directory_cache'):
        runtime._radio_directory_cache = {}
        runtime._radio_directory_pending = {}
    cache = runtime._radio_directory_cache
    cached = cache.get(key)
    if cached and cached[0] > time.monotonic():
        return station_items(runtime,copy.deepcopy(cached[1]))
    async def fetch():
        params={'hidebroken':'true','limit':str(limit),'order':'votes','reverse':'true'}
        if query: params['name']=query
        if country and country != 'ALL': params['countrycode']=country
        if tag: params.update(tag=tag,tagExact='true')
        session=async_get_clientsession(runtime.hass)
        async with session.get('https://de1.api.radio-browser.info/json/stations/search',params=params,timeout=ClientTimeout(total=10),headers={'Accept':'application/json','User-Agent':'HOMEii-Flow-Engine/1.0'}) as response:
            response.raise_for_status()
            data=await response.json()
        if not isinstance(data,list): raise ValueError('Invalid Radio Browser response')
        cache[key]=(time.monotonic()+300,data[:limit])
        while len(cache)>32: cache.pop(next(iter(cache)))
        return data[:limit]
    if key not in runtime._radio_directory_pending:
        runtime._radio_directory_pending[key]=asyncio.create_task(fetch())
    task=runtime._radio_directory_pending[key]
    try:
        data=await asyncio.shield(task)
        return station_items(runtime,data)
    finally:
        if task.done(): runtime._radio_directory_pending.pop(key,None)
