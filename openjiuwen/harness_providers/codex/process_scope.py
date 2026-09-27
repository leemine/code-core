# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Private Linux CLI scope; run by absolute filename, without importing core.

Only this dedicated process becomes a child subreaper. A successful exit is
proof that waitpid reported ECHILD after all owned descendants were stopped.
The parent must not treat a timeout, a signal exit, or a nonzero exit as proof.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_CLEANUP_SECONDS = 1.0
_POLL_SECONDS = 0.005
_PR_SET_CHILD_SUBREAPER = 36


def _enable_subreaper() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Linux child subreaper is required")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")


def _stop_children() -> None:
    # This wrapper is single-threaded. No handler reaps children, so a PID
    # read here cannot be recycled before kill: even an exited child remains
    # our unreaped zombie. Never signal a process group or an ancestor.
    children = Path(f"/proc/self/task/{os.getpid()}/children").read_text()
    for child in children.split():
        try:
            os.kill(int(child), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _reap_children() -> bool:
    """Return True only when the kernel confirms there are no children."""
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def _cleanup() -> None:
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while True:
        _stop_children()
        if _reap_children():
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("owned descendant exit was not confirmed")
        # Killing a parent reparents its children to this subreaper. The next
        # iteration stops and reaps that next generation, including setsid
        # children and descendants left behind by a naturally exited CLI.
        time.sleep(_POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--" or len(args) < 2:
        print("codex process scope: expected -- CLI [args]", file=sys.stderr)
        return 2
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        _enable_subreaper()
        # Inherit the exact SDK streams. No event parsing or proxy protocol.
        process = subprocess.Popen(args[1:])
        try:
            while not stopping:
                pid, status = os.waitpid(process.pid, os.WNOHANG)
                if pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
                    break
                time.sleep(_POLL_SECONDS)
        finally:
            _cleanup()
            # _cleanup may have reaped the CLI after an explicit stop. Its
            # exit code is not the scope exit authority: ECHILD is.
            if process.returncode is None:
                process.returncode = 0
    except Exception as exc:  # Fail closed without logging CLI arguments.
        print(f"codex process scope: {type(exc).__name__}: exit not confirmed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
