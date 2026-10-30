import asyncio
import os
import signal
import sys
from pathlib import Path

from core.settings import Settings
from worker.main import run

BACKEND_DIR = Path(__file__).parents[1]


async def test_worker_exits_when_stop_is_set(settings: Settings) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run(settings, stop))
    await asyncio.sleep(0.2)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_worker_process_exits_cleanly_on_sigterm(settings: Settings) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "worker.main",
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": str(settings.database_url)},
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    try:
        async with asyncio.timeout(10):
            while b"worker started" not in (line := await proc.stderr.readline()):
                assert line, "worker exited before starting"
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(5):
            stderr = await proc.stderr.read()
            assert await proc.wait() == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert b"worker stopping" in stderr
