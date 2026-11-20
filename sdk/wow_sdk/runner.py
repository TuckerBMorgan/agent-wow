"""One bounded control execution; observers never acquire or release control."""
import asyncio
import time

from .models import RunResult, record
from .sdk import SDK, deadline


def run_script(main, *, timeout_s=60, mode="control"):
    """Run an async main(bot, sdk) callback against an already logged-in character."""
    if not callable(main) or timeout_s <= 0:
        raise ValueError("Provide an async callback and a positive timeout_s")
    return asyncio.run(_run(main, timeout_s, mode))


async def _run(main, timeout_s, mode):
    from .bot import Bot

    sdk = SDK(mode=mode)
    started = time.monotonic()
    scope = deadline.set(started + timeout_s)
    result = RunResult(False)
    try:
        async with asyncio.timeout(timeout_s):
            await sdk._start(min(timeout_s, 10))
            result.result = await main(Bot(sdk), sdk)
            sdk._check()
            result.success = True
    except (Exception, asyncio.CancelledError) as error:
        result.error = str(error) or type(error).__name__
    finally:
        result.final_state = record(sdk._state, cls=dict) if sdk._state else None
        try:
            await sdk._close()
        except Exception as error:
            result.success = False
            result.error = result.error or f"Cleanup failed: {error}"
        result.duration_s = time.monotonic() - started
        result.logs = sdk.logs[:]
        deadline.reset(scope)
    return result
