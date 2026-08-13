import argparse
import os
import signal
from pathlib import Path

from grader.qemu_backend import QemuCaseBackend
from grader.vm_launcher import UnixVmLauncherServer


class _Shutdown(BaseException):
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Credentialless one-case QEMU launcher")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--allow-uid", action="append", type=int, default=[])
    arguments = parser.parse_args()
    allowed_uids = set(arguments.allow_uid or [os.getuid()])
    backend = QemuCaseBackend(
        rootfs=arguments.assets / "rootfs.ext4",
        kernel=arguments.assets / "vmlinuz",
        initramfs=arguments.assets / "initramfs",
    )

    def stop_on_signal(signum: int, frame: object) -> None:
        raise _Shutdown

    previous_handlers = {
        signum: signal.signal(signum, stop_on_signal)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        with UnixVmLauncherServer(
            arguments.socket,
            backend=backend,
            allowed_uids=allowed_uids,
        ) as server:
            try:
                server.serve_forever(poll_interval=0.5)
            except _Shutdown:
                pass
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
