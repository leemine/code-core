# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OC0 Linux source/ownership qualification, not production Provider code.

Uses the existing real CLI + loopback fixture. No install or external model.
All malicious inputs below are synthetic and stay inside task-owned storage.
"""
import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import socket
import stat
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from _opencode_server_probe import Probe
from aiohttp import web

CLI_SHA256 = "bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54"


def source_digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class Rejected(RuntimeError):
    """Probe-only admission/startup failure with a nonsecret reason."""


def snapshot(path):
    result = {}
    for item in [path, *sorted(path.rglob("*"))]:
        meta = item.lstat()
        if stat.S_ISLNK(meta.st_mode) or not (stat.S_ISDIR(meta.st_mode) or stat.S_ISREG(meta.st_mode)):
            raise Rejected("source_symlink_or_special_file")
        if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) & 0o222:
            raise Rejected("source_not_sealed")
        result[str(item.relative_to(path))] = [
            stat.S_IMODE(meta.st_mode),
            hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None,
        ]
    return result


def group_members(pgid):
    members = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid:
                members[int(path.parent.name)] = fields[0]  # state, including zombies
        except (FileNotFoundError, ProcessLookupError):
            pass
    return members


class IsolationProbe(Probe):
    async def start(self):
        # Base setup builds the fixture; this probe starts only after sealing.
        return None

    async def prepare(self):
        await self.setup()
        for name in ("home", "config", "data", "cache", "state", "tmp", "work"):
            (self.root / name).chmod(0o700)
        (self.root / "config/opencode/.gitignore").chmod(0o400)
        (self.root / "config/opencode").chmod(0o500)
        (self.root / "config").chmod(0o500)
        (self.root / "home").chmod(0o500)
        self.sealed = {name: snapshot(self.root / name) for name in ("home", "config")}
        self.expected_env = dict(self.env)
        self.lock = None
        self.launch_count = 0
        self.generation = 0
        self.groups = []
        self.stop_records = []
        self.unit = None
        self.cgroup = None
        self.server_pid = None

    def preflight(self, *, managed=Path("/etc/opencode")):
        if os.path.lexists(managed):
            raise Rejected("managed_source_present")
        if self.env != self.expected_env:
            raise Rejected("environment_drift")
        if hashlib.sha256(self.cli.read_bytes()).hexdigest() != CLI_SHA256:
            raise Rejected("binary_digest_mismatch")
        if any(snapshot(self.root / name) != expected for name, expected in self.sealed.items()):
            raise Rejected("source_content_drift")
        for name in ("data", "cache", "state", "tmp"):
            path = self.root / name
            meta = path.lstat()
            if path.is_symlink() or meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o700:
                raise Rejected("runtime_path_not_private")
        # Fresh private auth is allowed; inherited well-known auth can fetch remote config.
        if os.path.lexists(self.root / "data/opencode/auth.json"):
            raise Rejected("unadmitted_auth_source")

    async def launch(self, *, port=None, config=None):
        self.preflight()
        lock = (self.root / "owner.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise Rejected("storage_already_owned") from None
        self.lock = lock
        if port is None:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.generation += 1
        log_path = self.root / f"server-{self.generation}.log"
        self.log = log_path.open("wb")
        env = dict(self.env)
        if config is not None:  # deliberate malformed native configuration test only
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        self.launch_count += 1
        self.unit = "r1-05-oc0-" + uuid.uuid4().hex + ".service"
        launch_file = self.root / f"launch-{self.generation}.json"
        launch_file.write_text(json.dumps({"cli": str(self.cli), "env": env,
                                           "cwd": str(self.root / "work"), "port": port}))
        launch_file.chmod(0o600)
        wrapper = (
            "import json,os,sys; d=json.load(open(sys.argv[1])); os.chdir(d['cwd']); "
            "os.execve(d['cli'], [d['cli'],'serve','--hostname','127.0.0.1','--port',str(d['port'])], d['env'])"
        )
        self.process = await asyncio.create_subprocess_exec(
            "systemd-run", "--user", "--quiet", "--pipe", "--wait", "--collect",
            "--unit=" + self.unit, "--service-type=exec", "-p", "KillMode=control-group",
            "-p", "TimeoutStopSec=2s", "-p", "RuntimeMaxSec=90s",
            "--", sys.executable, "-I", "-S", "-c", wrapper, str(launch_file),
            stdout=self.log, stderr=self.log, start_new_session=True,
        )
        self.groups.append(self.process.pid)
        try:
            async with asyncio.timeout(20):
                while True:
                    if self.process.returncode is not None:
                        raise Rejected("process_start_failed")
                    if self.url in log_path.read_text(errors="replace"):
                        try:
                            status, health = await self.api("GET", "/global/health")
                            if status == 200 and health == {"healthy": True, "version": "1.18.18"}:
                                break
                        except aiohttp.ClientError:
                            pass
                    await asyncio.sleep(0.05)
                properties = await self.unit_properties()
                self.cgroup = properties["ControlGroup"]
                self.server_pid = int(properties["MainPID"])
                if not self.cgroup or self.server_pid <= 0 or properties["KillMode"] != "control-group":
                    raise Rejected("supervisor_ownership_unverified")
                status, actual = await self.api("GET", "/config")
                if status != 200:
                    raise Rejected("effective_config_unavailable")
                keys = ("model", "small_model", "enabled_providers", "provider",
                        "permission", "mcp", "lsp", "formatter", "agent", "share", "autoupdate")
                expected = {**self.config, "agent": {
                    "title": {"disable": True, "options": {}, "permission": {}},
                }}  # fixed 1.18.18 codec defaults; do not drop arbitrary extra fields
                if (any(actual.get(key) != expected[key] for key in keys)
                        or any(actual.get(key) for key in ("plugin", "command", "instructions", "skills"))):
                    self.save("config-readback-diff.json", {
                        "different_fields": [key for key in keys if actual.get(key) != expected[key]],
                        "source_fields": {key: actual.get(key) for key in
                                          ("agent", "share", "autoupdate", "command", "instructions", "skills")},
                    })
                    raise Rejected("effective_config_mismatch")
                self.preflight()
                await self.connect_events()
        except BaseException:
            await self.stop()
            raise

    async def control(self, *args):
        process = await asyncio.create_subprocess_exec(
            "systemctl", "--user", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return process.returncode, out.decode()

    async def unit_properties(self):
        _, output = await self.control("show", self.unit, "--property=MainPID,ControlGroup,KillMode,ActiveState,Result")
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def cgroup_empty(self):
        if not self.cgroup:
            return True
        root = Path("/sys/fs/cgroup") / self.cgroup.lstrip("/")
        return not root.exists() or all(not p.read_text().strip() for p in root.rglob("cgroup.procs"))

    async def stop(self):
        if self.pump:
            self.pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, aiohttp.ClientError):
                await self.pump
            self.pump = None
        if self.process:
            started = time.monotonic()
            if self.unit:
                properties = await self.unit_properties()
                self.cgroup = properties.get("ControlGroup") or self.cgroup
                await self.control("stop", self.unit)
            await asyncio.wait_for(self.process.wait(), 10)
            properties = await self.unit_properties()
            if not self.cgroup_empty() or properties.get("ActiveState") in ("active", "activating", "deactivating"):
                raise Rejected("owned_exit_unconfirmed")  # retain handle + storage lease for retry
            self.stop_records.append({"generation": self.generation, "unit": self.unit,
                                      "seconds": round(time.monotonic() - started, 3),
                                      "exit_code": self.process.returncode, "cgroup_empty": True,
                                      "active_state": properties.get("ActiveState", "collected")})
            self.process = None
            self.unit = None
            self.cgroup = None
            self.server_pid = None
        if self.log:
            self.log.close()
            self.log = None
        if getattr(self, "lock", None):
            self.lock.close()
            self.lock = None

    def rejects(self, name, fn):
        launches = self.launch_count
        try:
            fn()
        except Rejected as exc:
            self.check(name, self.launch_count == launches, str(exc))
        else:
            self.check(name, False)

    async def negatives(self):
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_TEST_HOME",
                    "OPENCODE_TEST_MANAGED_CONFIG_DIR", "OPENCODE_PERMISSION", "NODE_OPTIONS", "OPENAI_API_KEY"):
            self.env[key] = "synthetic-untrusted-value"
            self.rejects("reject_env_" + key, self.preflight)
            self.env = dict(self.expected_env)
        managed = self.root / "managed-canary"
        managed.mkdir()
        self.rejects("reject_managed_directory", lambda: self.preflight(managed=managed))
        managed.rmdir()
        managed.symlink_to(self.root / "missing-target")
        self.rejects("reject_managed_dangling_symlink", lambda: self.preflight(managed=managed))
        managed.unlink()
        source = self.root / "config/opencode/.gitignore"
        source.chmod(0o600)
        self.rejects("reject_writable_source", self.preflight)
        source.write_text("changed-content\n")
        source.chmod(0o400)
        self.rejects("reject_sealed_content_drift", self.preflight)
        source.chmod(0o600)
        source.write_text("node_modules\n")
        source.chmod(0o400)
        home = self.root / "home"
        home.chmod(0o700)
        (home / ".opencode").symlink_to(self.root / "work/.opencode", target_is_directory=True)
        home.chmod(0o500)
        self.rejects("reject_home_discovery_symlink", self.preflight)
        home.chmod(0o700)
        (home / ".opencode").unlink()
        home.chmod(0o500)
        auth = self.root / "data/opencode/auth.json"
        auth.parent.mkdir(exist_ok=True, parents=True)
        auth.write_text('{"https://untrusted.invalid":{"type":"wellknown","key":"TOKEN","token":"synthetic"}}')
        self.rejects("reject_wellknown_auth", self.preflight)
        auth.unlink()
        data = self.root / "data"
        data.chmod(0o755)
        self.rejects("reject_nonprivate_data_root", self.preflight)
        data.chmod(0o700)
        temporary = self.root / "tmp"
        temporary.rename(self.root / "tmp-real")
        temporary.symlink_to(self.root / "tmp-real", target_is_directory=True)
        self.rejects("reject_runtime_root_symlink", self.preflight)
        temporary.unlink()
        (self.root / "tmp-real").rename(temporary)
        original = self.cli
        self.cli = self.root / "wrong-cli"
        self.cli.write_bytes(b"not the pinned binary")
        self.rejects("reject_binary_drift", self.preflight)
        self.cli = original
        self.preflight()
        self.check("negative_preflights_spawned_nothing", self.launch_count == 0)


async def run(cli, output):
    a = IsolationProbe(cli, output)
    b = IsolationProbe(cli, Path(output) / "peer")
    foreign = None
    try:
        await a.prepare()
        await b.prepare()
        await a.negatives()
        await a.launch()
        await b.launch()
        a.check("separate_private_roots_and_ports", a.root != b.root and a.url != b.url)
        sid = await a.new()
        await a.submit(sid, "OC0 isolated session")
        messages = await a.terminal(sid)
        a.check("sealed_config_real_turn", any(p.get("text") == "OC0-LOCAL-OK" for m in messages for p in m["parts"]))
        async with b.client.get(a.url + "/global/health") as response:
            a.check("peer_credential_rejected", response.status == 401)
        status, _ = await b.api("GET", f"/session/{sid}")
        a.check("peer_cannot_read_session", status == 404, status)
        async with aiohttp.ClientSession() as anonymous:
            async with anonymous.get(a.url + "/global/health") as response:
                a.check("anonymous_rejected", response.status == 401)
        before = a.launch_count
        try:
            await a.launch()
        except Rejected as exc:
            a.check("same_storage_second_owner_rejected",
                    str(exc) == "storage_already_owned" and a.launch_count == before)
        else:
            a.check("same_storage_second_owner_rejected", False)
        a.preflight()
        a.check("sealed_source_unchanged_after_turn", True)
        a.check("no_dependency_install_after_turn", not list((a.root / "config").rglob("node_modules")))
        await a.stop()
        a.check("peer_survives_other_stop", (await b.api("GET", "/global/health"))[0] == 200)
        await a.launch()
        status, restored = await a.api("GET", f"/session/{sid}/message")
        a.check("same_private_storage_restart_preserves_messages", status == 200 and restored == messages)
        await a.stop()

        foreign_requests = []

        async def healthy(request):
            foreign_requests.append(request.path)
            return web.json_response({"healthy": True, "version": "1.18.18"})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", healthy)
        foreign = web.AppRunner(app)
        await foreign.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        await web.SockSite(foreign, sock).start()
        try:
            await a.launch(port=port)
        except Rejected as exc:
            a.check("occupied_port_fails_without_attach", str(exc) == "process_start_failed" and not foreign_requests)
        else:
            a.check("occupied_port_fails_without_attach", False)
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/global/health") as response:
                a.check("foreign_listener_survives_cleanup", response.status == 200)
        try:
            await a.launch(config={**a.config, "permission": {"*": "INVALID"}})
        except Rejected as exc:
            a.check("health_is_not_config_readiness", str(exc) == "effective_config_unavailable", str(exc))
        else:
            a.check("health_is_not_config_readiness", False)
        a.check("failed_start_releases_owned_resources", a.process is None and a.lock is None)

        await a.launch()
        child_file = a.root / "work/owned-child.pid"
        a.actions = [{"tool": "bash", "args": {
            "command": f"/bin/sh -c 'trap \"\" TERM; echo $$ > {child_file}; while :; do sleep 1; done'",
            "description": "OC0 owned stubborn tool descendant", "timeout": 20000,
        }}]
        sid = await a.new(permission=[{"permission": "*", "pattern": "*", "action": "allow"}])
        await a.submit(sid, "Run only the prescribed owned local tool.")
        async with asyncio.timeout(15):
            while not child_file.exists():  # noqa: ASYNC110 -- external process file, no in-process event
                await asyncio.sleep(0.05)
        child = int(child_file.read_text())
        server_group = os.getpgid(a.server_pid)
        child_group = os.getpgid(child)
        a.check("tool_child_detaches_from_server_group", child_group != server_group)
        child_cgroup = await asyncio.to_thread(
            lambda: Path(f"/proc/{child}/cgroup").read_text().strip().split(":", 2)[2]
        )
        a.check("detached_tool_remains_in_owned_cgroup", child_cgroup == a.cgroup)
        await a.stop()
        a.check("stubborn_tool_exit_confirmed", not group_members(child_group))
        a.check("stubborn_tool_forced_stop_waited", a.stop_records[-1]["seconds"] >= 1.5)
        await a.stop()
        a.check("repeated_cleanup_is_idempotent", a.process is None and a.lock is None)
        a.check("peer_survives_descendant_cleanup", (await b.api("GET", "/global/health"))[0] == 200)
        await a.launch()
        child_file.unlink()
        a.actions = [{"tool": "bash", "args": {
            "command": f"/bin/sh -c 'trap \"\" TERM; echo $$ > {child_file}; while :; do sleep 1; done'",
            "description": "OC0 owned tool during server crash", "timeout": 20000,
        }}]
        sid = await a.new(permission=[{"permission": "*", "pattern": "*", "action": "allow"}])
        await a.submit(sid, "Run only the prescribed local crash fixture.")
        async with asyncio.timeout(15):
            while not child_file.exists():  # noqa: ASYNC110 -- external process marker
                await asyncio.sleep(0.05)
        child_group = os.getpgid(int(child_file.read_text()))
        # Kill only the recorded MainPID of our transient unit; supervisor owns descendants.
        a.check("crash_main_pid_owned", (await a.unit_properties())["MainPID"] == str(a.server_pid))
        os.kill(a.server_pid, signal.SIGKILL)
        await asyncio.wait_for(a.process.wait(), 10)
        a.check("server_crash_supervisor_reaps_detached_tool", not group_members(child_group) and a.cgroup_empty())
        await a.stop()
        a.check("crash_cleanup_releases_storage_lease", a.lock is None)
        a.check("peer_survives_server_crash", (await b.api("GET", "/global/health"))[0] == 200)
        a.check("all_owned_groups_empty", not any(group_members(pgid) for pgid in a.groups))
    finally:
        try:
            await a.close()
        finally:
            await b.close()
            if foreign:
                await foreign.cleanup()
        a.save("ownership.json", {"stops": a.stop_records, "peer_stops": b.stop_records,
                                  "roots": [str(a.root), str(b.root)],
                                  "source_sha256": source_digest(),
                                  "base_probe_sha256": hashlib.sha256(Path(__file__).with_name(
                                      "_opencode_server_probe.py").read_bytes()).hexdigest(),
                                  "cli_sha256": hashlib.sha256(a.cli.read_bytes()).hexdigest()})


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default=str(Path.home() / ".opencode/bin/opencode"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    async with asyncio.timeout(150):
        await run(args.cli, args.output)


if __name__ == "__main__":
    asyncio.run(main())
