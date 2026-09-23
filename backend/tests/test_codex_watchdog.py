from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex-watchdog.sh"


def _run_watchdog(tmp_path: Path, heartbeat_age: int) -> tuple[dict, str]:
    runtime = tmp_path / "runtime"
    heartbeat = runtime / "codex-runtime" / "codex-worker-heartbeat.json"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text('{"status":"idle"}\n', encoding="utf-8")
    timestamp = time.time() - heartbeat_age
    os.utime(heartbeat, (timestamp, timestamp))
    launch_log = tmp_path / "launchctl.log"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        f'#!/bin/zsh\nprint -- "$@" >> {str(launch_log)!r}\n', encoding="utf-8"
    )
    fake_launchctl.chmod(0o700)
    environment = {
        **os.environ,
        "FANTASY_ENV_FILE": str(tmp_path / "missing.env"),
        "FANTASY_RUNTIME_DIR": str(runtime),
        "FANTASY_LAUNCHCTL_BIN": str(fake_launchctl),
        "CODEX_WORKER_STALE_SECONDS": "300",
    }
    subprocess.run(["/bin/zsh", str(SCRIPT)], env=environment, check=True)
    report = json.loads((runtime / "codex-watchdog.json").read_text(encoding="utf-8"))
    calls = launch_log.read_text(encoding="utf-8") if launch_log.exists() else ""
    return report, calls


def test_codex_watchdog_leaves_a_fresh_worker_alone(tmp_path: Path) -> None:
    report, calls = _run_watchdog(tmp_path, heartbeat_age=30)
    assert report["status"] == "healthy"
    assert report["restart_attempted"] is False
    assert calls == ""


def test_codex_watchdog_restarts_a_stale_worker(tmp_path: Path) -> None:
    report, calls = _run_watchdog(tmp_path, heartbeat_age=360)
    assert report["status"] == "restarted"
    assert report["restart_attempted"] is True
    assert "kickstart -k gui/" in calls
    assert calls.rstrip().endswith("/com.jimai.fantasy-codex-agent")
