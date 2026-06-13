#!/usr/bin/env python3
"""Drop root before running the app, while staying bind-mount friendly.

The container starts as root only long enough to make the data directories
(including freshly created host bind mounts, which arrive owned by the host
user) writable by the unprivileged ``appuser``, then permanently drops to that
user and execs the real command (uvicorn). Running the network-facing app as a
non-root user limits the blast radius of any compromise.

If the container is already started as non-root (e.g. compose ``user:`` set, or
a read-only rootfs), the chown/drop steps are skipped and the command runs as
whatever user we are.
"""
import os
import pwd
import sys

DATA_DIR = os.getenv("DATA_DIR", "/data")
RUN_AS = os.getenv("RUN_AS_USER", "appuser")


def _chown_tree(path: str, uid: int, gid: int) -> None:
    for root, dirs, files in os.walk(path):
        for name in (root, *(os.path.join(root, n) for n in dirs + files)):
            try:
                os.chown(name, uid, gid)
            except OSError:
                pass  # best-effort: some mounts disallow chown


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        sys.exit("docker-entrypoint: no command given")

    if os.geteuid() == 0:
        try:
            ent = pwd.getpwnam(RUN_AS)
        except KeyError:
            os.execvp(argv[0], argv)  # user missing — run as-is rather than fail
            return
        if os.path.isdir(DATA_DIR):
            _chown_tree(DATA_DIR, ent.pw_uid, ent.pw_gid)
        os.setgid(ent.pw_gid)
        os.setuid(ent.pw_uid)
        os.environ["HOME"] = ent.pw_dir

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
