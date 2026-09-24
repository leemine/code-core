# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Private stdlib-only service entrypoint (executed with Python -I -S).

The service, not the host, holds the native lease. A delayed systemd start
cannot launch an old generation after host recovery invalidates its descriptor.
Secrets live only in a private launch file and the child's environment.
"""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path


def main(path):
    launch = json.loads(Path(path).read_text())
    scope = Path(path).parent.parent
    fd = os.open(scope / "native.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = json.loads((scope / "owner.json").read_text())
        if owner != launch["owner"]:
            return 73
        os.umask(0o077)
        return subprocess.call(
            [launch["cli"], "serve", "--hostname", "127.0.0.1", "--port", str(launch["port"])],
            cwd=launch["cwd"],
            env=launch["env"],
            close_fds=True,
        )
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        result = main(sys.argv[1])
    except Exception:
        # Never expose JSON, credentials or local paths in supervisor errors.
        result = 74
    sys.exit(result)
