# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Admission for the fixed Linux CLI's configuration discovery surfaces."""

import hashlib
import os
import stat
from pathlib import Path

from .config import CLI_SHA256
from .errors import OpenCodeError


def private_directory(path: Path):
    meta = path.lstat()
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.geteuid()
        or stat.S_IMODE(meta.st_mode) != 0o700
        or path.resolve() != path
    ):
        raise OpenCodeError("runtime_path_not_private", category="process_start_failed")


def sealed_snapshot(path):
    result = {}
    for item in (path, *sorted(path.rglob("*"))):
        meta = item.lstat()
        if (
            not (stat.S_ISREG(meta.st_mode) or stat.S_ISDIR(meta.st_mode))
            or meta.st_uid != os.geteuid()
            or stat.S_IMODE(meta.st_mode) & 0o277
        ):
            raise OpenCodeError("source_not_sealed", category="process_start_failed")
        result[str(item.relative_to(path))] = (
            stat.S_IMODE(meta.st_mode),
            hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None,
        )
    return result


class Sources:
    def __init__(self, root, cli, *, persistent_root=None):
        self.root, self.cli = root, cli
        self.persistent_root = persistent_root or root
        for name in ("home", "config", "cache", "tmp"):
            (root / name).mkdir(mode=0o700)
        if self.persistent_root is not root:
            self.persistent_root.mkdir(mode=0o700, exist_ok=True)
        for name in ("data", "state"):
            (self.persistent_root / name).mkdir(mode=0o700, exist_ok=True)
        directory = root / "config/opencode"
        directory.mkdir(mode=0o700)
        (directory / ".gitignore").write_text("*\n")
        (directory / ".gitignore").chmod(0o400)
        directory.chmod(0o500)
        for name in ("home", "config"):
            (root / name).chmod(0o500)
        self.sealed = {name: sealed_snapshot(root / name) for name in ("home", "config")}

    def verify(self):
        if os.path.lexists("/etc/opencode"):
            raise OpenCodeError("managed_source_present", category="process_start_failed")
        with self.cli.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != CLI_SHA256:
            raise OpenCodeError("binary_digest_mismatch", category="process_start_failed")
        if any(sealed_snapshot(self.root / name) != expected for name, expected in self.sealed.items()):
            raise OpenCodeError("source_drift", category="process_start_failed")
        private_directory(self.root)
        for name in ("cache", "tmp"):
            private_directory(self.root / name)
        private_directory(self.persistent_root)
        for name in ("data", "state"):
            private_directory(self.persistent_root / name)
        if os.path.lexists(self.persistent_root / "data/opencode/auth.json"):
            raise OpenCodeError("unadmitted_auth_source", category="process_start_failed")
