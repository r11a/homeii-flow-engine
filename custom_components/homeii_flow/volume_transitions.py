"""Optional wake volume fades owned by the Engine, never by the browser."""
import asyncio
import time


def cancel_transition(runtime, player):
    task = getattr(runtime, "_volume_transition_tasks", {}).pop(player, None)
    if task and not task.done():
        task.cancel()


def start_transition(runtime, player, target, seconds):
    cancel_transition(runtime, player)
    if not hasattr(runtime, "_volume_transition_tasks"):
        runtime._volume_transition_tasks = {}
    task = runtime.hass.async_create_task(run_transition(runtime, player, target, seconds))
    runtime._volume_transition_tasks[player] = task
    def finished(completed):
        if runtime._volume_transition_tasks.get(player) is completed:
            runtime._volume_transition_tasks.pop(player, None)
        if not completed.cancelled():
            completed.exception()  # Failure is recorded by the runner.
    task.add_done_callback(finished)


async def run_transition(runtime, player, target, seconds):
    target = max(0, min(100, float(target))) / 100
    seconds = max(1, min(600, float(seconds)))
    started = time.monotonic()
    sent = [0.0]
    try:
        while True:
            await asyncio.sleep(min(2, seconds))
            state = runtime.hass.states.get(player)
            if state is None or state.state != "playing":
                return
            current = state.attributes.get("volume_level")
            # Respect a manual change, while allowing delayed HA state echoes.
            if isinstance(current, (int, float)) and all(abs(current-value) > .015 for value in sent):
                return
            progress = min(1, (time.monotonic()-started)/seconds)
            level = target * progress
            for rule in runtime.active_volume_rule_summaries():
                if rule.get("player") == player:
                    level = min(level, max(0, min(100, int(rule.get("max_volume") or 0))) / 100)
            await runtime.async_call_service_response("media_player", "volume_set", {"entity_id":player,"volume_level":level}, return_response=False)
            sent = [*sent[-2:], level]
            if progress >= 1:
                return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        await runtime.async_record_activity("volume_transition_failed", str(error), data={"player":player})
