# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real local subprocess checks for the private Linux Codex scope."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper"), pytest.mark.timeout(15)]
_SCOPE = Path(__file__).resolve().parents[3] / "openjiuwen/harness_providers/codex/process_scope.py"
_TREE = """
import os, pathlib, signal, subprocess, sys, time
root, level, natural, detached = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if detached == 'yes':
    os.setsid()
(root / ('pid-' + str(level))).write_text(str(os.getpid()))
if level:
    subprocess.Popen([sys.executable, __file__, str(root), str(level-1), 'no', detached])
if natural == 'yes':
    while len(list(root.glob('pid-*'))) != 4:
        time.sleep(.005)
    sys.exit(0)
time.sleep(120)
"""


@pytest.fixture
def tree_scope(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(_TREE)
    processes = []
    pidfds = []

    def start(*, natural=False, detached=False):
        process = subprocess.Popen(
            [sys.executable, str(_SCOPE), "--", sys.executable, str(script), str(tmp_path),
             "3", "yes" if natural else "no", "yes" if detached else "no"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        processes.append(process)
        deadline = time.monotonic() + 5
        while len(list(tmp_path.glob("pid-*"))) != 4:
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "child tree did not start"
            time.sleep(.005)
        pids = [int(path.read_text()) for path in tmp_path.glob("pid-*")]
        for pid in pids:
            try:
                pidfds.append(os.pidfd_open(pid))
            except ProcessLookupError:
                # Natural-exit cleanup may already have reaped this child.
                assert natural
        return process, pids

    yield start
    # Exact owned PID handles remain valid even when a test fails. No group
    # signals and no global reaping/subreaper changes in the pytest process.
    for fd in pidfds:
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.communicate(timeout=3)


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("detached", [False, True], ids=["same-session", "setsid-descendants"])
def test_stop_reaps_slow_descendants_ignoring_term(tree_scope, stop_signal, detached):
    process, pids = tree_scope(detached=detached)
    process.send_signal(stop_signal)
    _stdout, stderr = process.communicate(timeout=3)
    assert process.returncode == 0, stderr
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)


def test_natural_cli_exit_reaps_background_descendants(tree_scope):
    process, pids = tree_scope(natural=True, detached=True)
    _stdout, stderr = process.communicate(timeout=3)
    assert process.returncode == 0, stderr
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)


def test_unrelated_process_in_same_group_survives(tree_scope):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        process, _pids = tree_scope()
        assert os.getpgid(unrelated.pid) == os.getpgid(process.pid)
        process.terminate()
        _stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


def test_inherits_stdio_without_protocol_proxy():
    process = subprocess.run(
        [sys.executable, str(_SCOPE), "--", sys.executable, "-c",
         "import sys; print(sys.stdin.readline().strip()); print('diagnostic', file=sys.stderr)"],
        input=b"opaque-json-rpc\n", capture_output=True, timeout=3, check=False,
    )
    assert process.returncode == 0
    assert process.stdout == b"opaque-json-rpc\n"
    assert process.stderr == b"diagnostic\n"


@pytest.mark.parametrize("failure", ["unsupported", "timeout", "enumeration"])
def test_scope_failures_never_report_success(failure):
    # Failure injection runs in a disposable process, never making pytest a
    # subreaper or changing the host signal handlers. No CLI is spawned here.
    code = """
import runpy, sys
scope = runpy.run_path(sys.argv[1])
main = scope['main']
state = main.__globals__
if sys.argv[2] == 'unsupported':
    state['sys'].platform = 'unsupported'
elif sys.argv[2] == 'timeout':
    state['_CLEANUP_SECONDS'] = 0
    state['_stop_children'] = lambda: None
    state['_reap_children'] = lambda: False
else:
    def denied():
        raise PermissionError('children unavailable')
    state['_stop_children'] = denied
if sys.argv[2] != 'unsupported':
    state['_enable_subreaper'] = state['_cleanup']
sys.exit(main(['--', '/must-not-be-started']))
"""
    result = subprocess.run([sys.executable, "-c", code, str(_SCOPE), failure],
                            capture_output=True, timeout=3, check=False)
    assert result.returncode != 0
    assert b"exit not confirmed" in result.stderr


def test_start_failure_is_nonzero():
    result = subprocess.run([sys.executable, str(_SCOPE), "--", "/missing-codex-cli"],
                            capture_output=True, timeout=3, check=False)
    assert result.returncode != 0
    assert b"exit not confirmed" in result.stderr
