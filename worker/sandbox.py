"""
Per-task sandbox setup and teardown.
Each task gets an isolated directory under /tmp/sandbox/{task_id}/
so scratch.md, logs, and temp files never cross between tasks.
"""
import shutil
from pathlib import Path


def setup(task_id: str) -> str:
    """Create sandbox directory and return its path."""
    sandbox = Path(f"/tmp/sandbox/{task_id}")
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "logs").mkdir(exist_ok=True)
    return str(sandbox)


def teardown(task_id: str) -> None:
    """Remove sandbox directory after task completion."""
    sandbox = Path(f"/tmp/sandbox/{task_id}")
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
