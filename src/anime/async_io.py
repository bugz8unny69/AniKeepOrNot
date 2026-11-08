"""Shared cancellation-drained blocking I/O for API and worker processes."""
import asyncio
import functools


async def blocking(callable_, *args, executor=None, **kwargs):
    """Cancellation waits for the thread before admission or ownership is dropped."""
    task = asyncio.get_running_loop().run_in_executor(executor, functools.partial(callable_, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                # A finished callable may itself raise CancelledError. There is
                # no outstanding thread to drain; never spin on its result.
                try:
                    task.result()
                except BaseException:
                    pass
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result
