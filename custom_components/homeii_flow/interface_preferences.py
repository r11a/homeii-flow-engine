"""Profile-scoped display preferences shared by card and HA actions."""
from __future__ import annotations
import copy
import re
from .const import DEFAULT_PROFILE_ID


def read_preferences(runtime, profile_id=None):
    profile = str(profile_id or DEFAULT_PROFILE_ID)
    return copy.deepcopy(runtime._storage.get("interface_preferences", {}).get(profile, {}))


def validate_preferences(payload, current):
    result = dict(current)
    if "night_mode" in payload:
        if payload["night_mode"] not in {"off", "on", "auto"}:
            raise ValueError("Invalid night mode")
        result["night_mode"] = payload["night_mode"]
    for key in ("night_start", "night_end"):
        if key in payload:
            if not isinstance(payload[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", payload[key]):
                raise ValueError("Invalid night time")
            result[key] = payload[key]
    if "night_days" in payload:
        days = payload["night_days"]
        if not isinstance(days, list) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ValueError("Invalid night days")
        result["night_days"] = list(dict.fromkeys(days))
    return result


async def save_preferences(runtime, payload):
    profile = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
    current = read_preferences(runtime, profile)
    result = validate_preferences(payload, current)
    store = runtime._storage.setdefault("interface_preferences", {})
    store[profile] = result
    try:
        await runtime.async_save()
    except Exception:
        if current: store[profile] = current
        else: store.pop(profile, None)
        raise
    return copy.deepcopy(result)


def read_wheel_preferences(runtime, profile_id, user_id):
    store = runtime._storage.get("wheel_preferences", {}).get(str(profile_id or DEFAULT_PROFILE_ID), {})
    return copy.deepcopy({"global": store.get("global", {}), "user": store.get("users", {}).get(user_id, {})})


async def save_wheel_preferences(runtime, payload, user_id, is_admin=False):
    scope = payload.get("scope", "user")
    if scope not in {"user", "global"} or (scope == "global" and not is_admin):
        raise ValueError("Global wheel settings require an administrator")
    context = payload.get("context", "")
    if not isinstance(context, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", context):
        raise ValueError("Invalid wheel context")
    preference = payload.get("preference", {})
    cleaned = {}
    for key in ("order", "hidden"):
        values = preference.get(key, [])
        if not isinstance(values, list) or len(values) > 500 or any(not isinstance(item, str) or len(item) > 256 for item in values):
            raise ValueError("Invalid wheel preferences")
        cleaned[key] = list(dict.fromkeys(values))
    profile = str(payload.get("profile_id") or DEFAULT_PROFILE_ID)
    store = runtime._storage.setdefault("wheel_preferences", {})
    previous = copy.deepcopy(store.get(profile, {}))
    current = copy.deepcopy(previous)
    target = current.setdefault("global", {}) if scope == "global" else current.setdefault("users", {}).setdefault(user_id, {})
    target[context] = cleaned
    store[profile] = current
    try:
        await runtime.async_save()
    except Exception:
        store[profile] = previous
        raise
    return read_wheel_preferences(runtime, profile, user_id)
