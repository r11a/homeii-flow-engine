"""Latest-selection scheduling without replaying or piling up MA mutations."""
import asyncio


class PlaybackRequests:
    def __init__(self):
        self._latest = {}
        self._locks = {}

    async def run(self, player, execute):
        ticket = object()
        self._latest[player] = ticket
        lock = self._locks.setdefault(player, asyncio.Lock())
        try:
            async with lock:
                if self._latest.get(player) is not ticket:
                    raise RuntimeError("A newer media selection replaced this request.")
                # MA has no cancel-message API once a mutation is dispatched.
                # Serialize it and drop superseded waiters before dispatch,
                # rather than letting every intermediate selection reach MA.
                return await execute()
        finally:
            if self._latest.get(player) is ticket:
                self._latest.pop(player, None)
