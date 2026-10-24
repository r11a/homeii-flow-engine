"""Copy a finite MA queue snapshot without stopping or clearing its source."""

async def copy_queue(runtime, source_id, target_id, source_queue, auto_play=True):
    if source_id == target_id:
        raise ValueError("Choose a player outside the source group")
    if source_queue.get("is_dynamic"):
        raise ValueError("Dynamic radio queues cannot be copied as a fixed queue")
    if not auto_play:
        raise ValueError("Queue copy currently starts playback on the target")
    count = int(source_queue.get("items") or 0)
    if not 0 < count <= 2000:
        raise ValueError("Queue copying supports 1–2000 items")
    items = []
    for offset in range(0, count, 500):
        response = await runtime.async_music_assistant_command({"command":"player_queues/items","args":{"queue_id":source_id,"limit":500,"offset":offset}})
        page = response.get("data") if isinstance(response, dict) else response
        if not isinstance(page, list):
            raise ValueError("Could not read the source queue")
        items.extend(page)
    media = [item.get("media_item") for item in items]
    if len(items) != count or any(not isinstance(item, dict) or not item.get("uri") for item in media):
        raise ValueError("Source queue contains items that cannot be copied")
    response = await runtime.async_music_assistant_command({"command":"player_queues/get","args":{"queue_id":source_id}})
    latest = response.get("data") if isinstance(response, dict) else response
    if not isinstance(latest, dict) or latest.get("items") != count or latest.get("current_index") != source_queue.get("current_index"):
        raise ValueError("Source queue changed while copying; please try again")
    index = max(0, min(count-1, int(source_queue.get("current_index") or 0)))
    await runtime.async_music_assistant_command({"command":"player_queues/play_media","args":{"queue_id":target_id,"media":media,"option":"replace","start_item":media[index]["uri"]}})
    return {"ok":True,"copied":True,"items":count,"source_queue_id":source_id,"target_queue_id":target_id,"auto_play":auto_play}
