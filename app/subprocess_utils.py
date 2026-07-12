"""Async subprocess timeout and cleanup helpers."""

import asyncio


async def reap_subprocess(process, *, terminate_first: bool = True) -> None:
    """Stop and reap a child process without leaving a zombie behind."""
    if process is None or getattr(process, "returncode", None) is not None:
        return
    action = getattr(process, "terminate" if terminate_first else "kill", None)
    wait = getattr(process, "wait", None)
    if not callable(action) or not callable(wait):
        return
    try:
        action()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(wait(), timeout=5)
    except asyncio.TimeoutError:
        kill = getattr(process, "kill", None)
        if callable(kill):
            try:
                kill()
            except ProcessLookupError:
                pass
        await wait()


async def communicate_with_timeout(process, timeout_seconds: float):
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        await reap_subprocess(process, terminate_first=True)
        raise RuntimeError(f"subprocess timed out after {timeout_seconds} seconds") from exc
    except asyncio.CancelledError:
        await reap_subprocess(process, terminate_first=False)
        raise
    except Exception:
        await reap_subprocess(process, terminate_first=False)
        raise


async def wait_with_timeout(process, timeout_seconds: float):
    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        await reap_subprocess(process, terminate_first=True)
        raise RuntimeError(f"subprocess timed out after {timeout_seconds} seconds") from exc
    except asyncio.CancelledError:
        await reap_subprocess(process, terminate_first=False)
        raise
    except Exception:
        await reap_subprocess(process, terminate_first=False)
        raise
