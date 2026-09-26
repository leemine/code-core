# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One private server and recoverable systemd cgroup lease per host scope.

This is resource ownership on a trusted Linux host, not a tool sandbox.
No attach, global kill, native retry or automatic dependency installation.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from .errors import OpenCodeError
from .native_plugins import stage_native_plugins, validate_native_plugin_packages
from .options import environment, native_config
from .source import Sources, private_directory


def write_private(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def lease(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        meta = os.fstat(fd)
        if meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise OpenCodeError("lease_not_private", category="process_start_failed")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


class ManagedServer:
    def __init__(self, config, context, *, skill_path=None):
        self.config, self.context = config, context
        self.skill_path = skill_path
        self.lock = self.process = self.log = self.owner = self.scope = None
        self._stop_lock = asyncio.Lock()
        self.native_config = self.plugin_stage = self.plugin_fingerprint = None

    async def control(self, *args):
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/systemctl",
            "--user",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), self.config.shutdown_timeout_s)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        return process.returncode, out.decode("utf-8", errors="replace")

    async def properties(self, owner):
        code, output = await self.control(
            "show",
            owner["unit"],
            "--property=LoadState,ActiveState,Description,ControlGroup,MainPID,KillMode",
        )
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if not values or (code and values.get("LoadState") != "not-found"):
            raise OpenCodeError("supervisor_unavailable", category="process_start_failed")
        if values.get("LoadState") != "not-found" and values.get("Description") != owner["description"]:
            raise OpenCodeError("service_identity_mismatch", category="process_start_failed")
        group = values.get("ControlGroup", "")
        if group and (
            not group.startswith(f"/user.slice/user-{os.geteuid()}.slice/")
            or not group.endswith("/" + owner["unit"])
            or ".." in group
        ):
            raise OpenCodeError("cgroup_identity_mismatch", category="process_start_failed")
        return values

    @staticmethod
    def group_empty(group):
        if not group:
            return True
        root = Path("/sys/fs/cgroup") / group.lstrip("/")
        try:
            return not root.exists() or all(not p.read_text().strip() for p in root.rglob("cgroup.procs"))
        except FileNotFoundError:
            return not root.exists()

    async def reap(self, owner):
        values = await self.properties(owner)
        group = values.get("ControlGroup", "")
        if values.get("LoadState") != "not-found":
            await self.control("stop", owner["unit"])
        async with asyncio.timeout(self.config.shutdown_timeout_s):
            while True:
                values = await self.properties(owner)
                if values.get("ActiveState") not in {
                    "active",
                    "activating",
                    "deactivating",
                    "reloading",
                } and self.group_empty(group):
                    return
                await asyncio.sleep(0.05)

    def read_owner(self):
        path = self.scope / "owner.json"
        if not os.path.lexists(path):
            return None
        meta = path.lstat()
        if path.is_symlink() or meta.st_uid != os.geteuid() or meta.st_mode & 0o077 or meta.st_size > 4096:
            raise OpenCodeError("invalid_owner_descriptor", category="process_start_failed")
        try:
            owner = json.loads(path.read_text())
            nonce = owner["generation"]
            if (
                set(owner) != {"generation", "unit", "description"}
                or not re.fullmatch(r"[0-9a-f]{32}", nonce)
                or owner["unit"] != f"ojw-opencode-{self.scope.name}-{nonce}.service"
                or owner["description"] != f"OpenJiuwen OpenCode {self.scope.name} {nonce}"
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise OpenCodeError("invalid_owner_descriptor", category="process_start_failed") from None
        return owner

    async def start(self):
        # These local admission checks precede service submission; keep lease
        # mutations synchronous so cancellation cannot race a worker thread.
        if sys.platform != "linux" or os.geteuid() == 0 or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():  # noqa: ASYNC240
            raise OpenCodeError("unsupported_supervisor_platform", category="process_start_failed")
        if not self.config.runtime_root or not self.context.cwd:
            raise OpenCodeError("explicit_runtime_and_cwd_required", category="process_start_failed")
        root = Path(self.config.runtime_root)
        private_directory(root)
        cwd = Path(self.context.cwd)
        if not cwd.is_absolute() or not cwd.is_dir():  # noqa: ASYNC240
            raise OpenCodeError("invalid_working_directory", category="process_start_failed")
        self.cwd = str(cwd.resolve())  # noqa: ASYNC240
        cli = self.config.cli_path or shutil.which("opencode")
        if not cli:
            raise OpenCodeError("cli_unavailable", category="process_start_failed")
        self.cli = Path(cli).resolve(strict=True)  # noqa: ASYNC240
        identity = json.dumps([self.context.agent_id, self.context.host_session_id, self.cwd], sort_keys=True)
        self.scope = root / hashlib.sha256(identity.encode()).hexdigest()[:32]
        self.scope.mkdir(mode=0o700, exist_ok=True)
        private_directory(self.scope)
        try:
            self.lock = lease(self.scope / "host.lock")
        except BlockingIOError:
            raise OpenCodeError("storage_already_owned", category="process_start_failed") from None
        self.plugin_fingerprint = (
            await asyncio.to_thread(validate_native_plugin_packages, self.config.native_plugins)
            if self.config.native_plugins is not None
            else None
        )
        # Product MCP endpoints and staged plugin wrapper paths are generation
        # local.  The frozen provider config already carries the stable plugin
        # source/version/digest/hook identity.
        # Keep the pre-OC4 stable identity byte-for-byte compatible while the
        # full generated config remains sealed and verified for this service.
        stable_native_config = native_config(
            self.config,
            self.context.host_capabilities,
            self.context.mcp_servers,
            skill_path=self.skill_path,
            plugin_specs=(),
            include_product_mcp=False,
        )
        config_identity = asdict(self.config)
        if config_identity.get("native_plugins") is None:
            # Preserve the OC1-OC6 storage identity for configurations that do
            # not opt into the new provider-private plugin surface.
            config_identity.pop("native_plugins")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"config": config_identity, "native": stable_native_config},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        identity_file = self.scope / "identity.json"
        if os.path.lexists(identity_file):  # noqa: ASYNC240
            if identity_file.is_symlink() or identity_file.read_text().strip() != json.dumps(fingerprint):
                raise OpenCodeError("storage_identity_mismatch", category="process_start_failed")
        else:
            write_private(identity_file, fingerprint)
        previous = self.read_owner()
        if previous:
            # Reap only the exact described service. Unknown ownership fails closed.
            await self.reap(previous)
        native_lock = lease(self.scope / "native.lock")
        try:
            nonce = uuid.uuid4().hex
            owner = {
                "generation": nonce,
                "unit": f"ojw-opencode-{self.scope.name}-{nonce}.service",
                "description": f"OpenJiuwen OpenCode {self.scope.name} {nonce}",
            }
            self.root = self.scope / nonce
            self.root.mkdir(mode=0o700)
            self.persistent_root = self.scope / "persistent"
            self.sources = Sources(self.root, self.cli, persistent_root=self.persistent_root)
            await asyncio.to_thread(self.sources.verify)
            self.plugin_stage = await asyncio.to_thread(
                stage_native_plugins,
                self.root,
                self.config.native_plugins,
                fingerprint=self.plugin_fingerprint,
            )
            self.native_config = native_config(
                self.config,
                self.context.host_capabilities,
                self.context.mcp_servers,
                skill_path=self.skill_path,
                plugin_specs=self.plugin_stage.specs,
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.url, self.password = f"http://127.0.0.1:{port}", uuid.uuid4().hex
            self.log_path = self.root / "server.log"
            self.log = os.fdopen(os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
            launch_file = self.root / "launch.json"
            write_private(
                launch_file,
                {
                    "owner": owner,
                    "cli": str(self.cli),
                    "cwd": self.cwd,
                    "port": port,
                    "env": environment(
                        self.root,
                        self.native_config,
                        self.password,
                        persistent_root=self.persistent_root,
                        allow_native_plugins=bool(self.plugin_stage.specs),
                    ),
                },
            )
            write_private(self.scope / "owner.json", owner)
            self.owner = owner
        finally:
            os.close(native_lock)
        self.process = await asyncio.create_subprocess_exec(
            "/usr/bin/systemd-run",
            "--user",
            "--quiet",
            "--pipe",
            "--wait",
            "--collect",
            "--unit=" + self.owner["unit"],
            "--service-type=exec",
            "-p",
            "KillMode=control-group",
            "-p",
            "TimeoutStopSec=2s",
            "-p",
            "Description=" + self.owner["description"],
            "--",
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).with_name("_launcher.py")),
            str(launch_file),
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
        )

    async def verify_running(self):
        if self.process is None or self.process.returncode is not None:
            raise OpenCodeError("server_exited", category="process_start_failed")
        values = await self.properties(self.owner)
        if (
            values.get("ActiveState") != "active"
            or values.get("KillMode") != "control-group"
            or not values.get("ControlGroup")
            or int(values.get("MainPID", 0)) <= 0
        ):
            raise OpenCodeError("supervisor_ownership_unverified", category="process_start_failed")
        await asyncio.to_thread(self.sources.verify)
        if self.plugin_stage is not None:
            await asyncio.to_thread(self.plugin_stage.verify_files)

    async def verify_native_plugin_inventory(self):
        if self.plugin_stage is None or not self.plugin_stage.inventories:
            return
        async with asyncio.timeout(self.config.startup_timeout_s):
            while not self.plugin_stage.inventory_ready():
                if self.process is None or self.process.returncode is not None:
                    raise OpenCodeError("server_exited", category="process_start_failed")
                await asyncio.sleep(0.05)
        await asyncio.to_thread(self.plugin_stage.verify_inventory)

    async def stop(self):
        async with self._stop_lock:
            if self.owner:
                await self.reap(self.owner)
            if self.process:
                await asyncio.wait_for(self.process.wait(), self.config.shutdown_timeout_s)
            if self.lock is not None:
                if self.owner:
                    native_lock = lease(self.scope / "native.lock")
                    try:
                        if self.read_owner() == self.owner:
                            (self.scope / "owner.json").unlink()
                        # A delayed old launcher can acquire the lease after this,
                        # but its descriptor will no longer match.
                    finally:
                        os.close(native_lock)
                os.close(self.lock)
                self.lock = None
            if self.log:
                self.log.close()
            self.owner = self.process = self.log = None
