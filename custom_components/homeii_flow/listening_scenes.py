"""Persistent listening presets composed from existing Engine operations.

No speculative rollback/retry of playback: MA may already have accepted a command.
"""
from __future__ import annotations
import asyncio
import copy
import uuid


def list_scenes(runtime, profile="default"):
    return copy.deepcopy(list(runtime._storage.get("listening_scenes", {}).get(profile, {}).values()))


async def save_scene(runtime, payload):
    profile = str(payload.get("profile_id") or "default")
    name = str(payload.get("name") or "").strip()
    player = str(payload.get("player") or "").strip()
    media = str(payload.get("media_id") or "").strip()
    members = payload.get("members", [])
    lights = payload.get("lights", [])
    if not name or len(name) > 120 or not player or not media or len(media) > 4096 or "://" not in media:
        raise ValueError("A name, player and valid media URI are required")
    if any(ord(c) < 32 for c in media):
        raise ValueError("Invalid media URI")
    if not isinstance(members, list) or len(members) > 32 or any(not isinstance(v,str) or not v.strip() for v in members):
        raise ValueError("Choose up to 32 group members")
    if not isinstance(lights, list) or len(lights) > 32 or any(not isinstance(v,str) or not v.startswith("light.") for v in lights):
        raise ValueError("Invalid light entities")
    volume = payload.get("volume")
    if volume is not None and (isinstance(volume,bool) or not isinstance(volume,(int,float)) or not 0 <= volume <= 100):
        raise ValueError("Volume must be between 0 and 100")
    store = runtime._storage.setdefault("listening_scenes", {}).setdefault(profile, {})
    key = str(payload.get("scene_id") or uuid.uuid4().hex)
    if payload.get("scene_id") and key not in store:
        raise ValueError("Scene no longer exists")
    if key not in store and len(store) >= 100:
        raise ValueError("A profile can contain up to 100 scenes")
    item = {"id":key,"name":name,"player":player,"media_id":media,
        "members":list(dict.fromkeys(v for v in members if v != player)),
        "lights":list(dict.fromkeys(lights)),"volume":volume,
        "profile_id":profile}
    previous = store.get(key)
    store[key] = item
    try: await runtime.async_save()
    except Exception:
        if previous is None: store.pop(key,None)
        else: store[key] = previous
        raise
    return copy.deepcopy(item)


async def delete_scene(runtime,payload):
    store=runtime._storage.get("listening_scenes",{}).get(str(payload.get("profile_id") or "default"),{})
    key=payload.get("scene_id")
    previous=store.pop(key,None)
    if previous is None: return {"deleted":False}
    try: await runtime.async_save()
    except Exception:
        store[key]=previous
        raise
    return {"deleted":True}


async def run_scene(runtime,payload):
    profile=str(payload.get("profile_id") or "default")
    item=copy.deepcopy(runtime._storage.get("listening_scenes",{}).get(profile,{}).get(payload.get("scene_id")))
    if not item: raise ValueError("Choose an existing listening scene")
    for player in [item["player"],*item["members"]]:
        if not runtime._player_readiness(player).get("ready"):
            raise ValueError(f"Scene player is unavailable: {player}")
    # A shared member must not be claimed by two scenes with different leaders.
    locks=getattr(runtime,"_scene_locks",None)
    if locks is None: locks={};runtime._scene_locks=locks
    player=item["player"]
    target_locks=[locks.setdefault(target,asyncio.Lock()) for target in sorted({player,*item["members"]})]
    if any(lock.locked() for lock in target_locks): raise ValueError("A scene is already starting on one of these players")
    for lock in target_locks: await lock.acquire()
    try:
        completed=[];step="group"
        try:
            if item["members"]:
                await runtime.async_apply_group({"owner":player,"members":item["members"]})
                completed.append(step)
            step="volume"
            if item["volume"] is not None:
                for target in [player,*item["members"]]:
                    await runtime.async_player_command({"player":target,"command":"volume","volume_level":item["volume"]/100})
                completed.append(step)
            step="playback"
            response=await runtime.async_play_media({"player":player,"media_id":item["media_id"],"enqueue":"replace","profile_id":profile})
            if isinstance(response,dict) and response.get("ok") is False:
                raise ValueError("Music Assistant did not accept playback")
            completed.append(step)
            step="lighting"
            if item["lights"]:
                await runtime.artwork_lighting.configure({"player":player,"lights":item["lights"],"enabled":True})
                completed.append(step)
            result={"ok":True,"scene_id":item["id"],"completed":completed}
        except Exception as err:
            result={"ok":False,"scene_id":item["id"],"completed":completed,"failed_step":step,"error":str(err)}
        try:
            await runtime.async_record_activity("scene_started" if result["ok"] else "scene_failed",item["name"],profile_id=profile,data=result)
        except Exception:
            result["warning"]="The scene result could not be saved to activity history"
        return result
    finally:
        for lock in target_locks: lock.release()
