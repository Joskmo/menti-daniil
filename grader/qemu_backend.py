import base64
import binascii
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

ImageBuilder = Callable[[Path, Path], None]
Hypervisor = Callable[[Path], bytes]
_RESULT_MARKER = re.compile(rb"MENTI_RESULT:([A-Za-z0-9+/=]+)(?:\r?\n|$)")


class QemuCaseBackend:
    def __init__(
        self,
        *,
        rootfs: str | Path,
        kernel: str | Path,
        initramfs: str | Path,
        image_builder: ImageBuilder | None = None,
        hypervisor: Hypervisor | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("timeout_seconds must be from 0 to 120")
        self.rootfs = _asset(rootfs, "rootfs")
        self.kernel = _asset(kernel, "kernel")
        self.initramfs = _asset(initramfs, "initramfs")
        self.image_builder = image_builder or self._build_input_image
        self.hypervisor = hypervisor or self._run_qemu
        self.timeout_seconds = timeout_seconds

    def __call__(
        self,
        source_files: tuple[dict[str, str], ...],
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="menti-qemu-case-") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            tree = root / "input"
            source = tree / "source"
            source.mkdir(parents=True, mode=0o700)
            request_path = tree / "request.json"
            _write_file(
                request_path,
                json.dumps(
                    execution,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8"),
                0o600,
            )
            for source_file in source_files:
                relative = _source_path(source_file["path"])
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_file(destination, source_file["content"].encode("utf-8"), 0o644)
            input_image = root / "input.ext4"
            self.image_builder(tree, input_image)
            if input_image.is_symlink() or not input_image.is_file():
                raise RuntimeError("input image builder produced no regular image")
            console = self.hypervisor(input_image)
        if not isinstance(console, bytes) or len(console) > 4_000_000:
            raise RuntimeError("QEMU console output is invalid or too large")
        matches = list(_RESULT_MARKER.finditer(console))
        if len(matches) != 1:
            raise RuntimeError("QEMU guest returned no valid result marker")
        try:
            payload = base64.b64decode(matches[0].group(1), validate=True)
            response = json.loads(payload.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("QEMU guest returned no valid result marker") from error
        if not isinstance(response, dict):
            raise RuntimeError("QEMU guest result is not an object")
        return response

    @staticmethod
    def _build_input_image(tree: Path, image: Path) -> None:
        environment = {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
        }
        try:
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext4",
                    "-d",
                    str(tree),
                    "-L",
                    "MENTI_INPUT",
                    str(image),
                    "16384",
                ],
                check=True,
                capture_output=True,
                timeout=15,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("failed to build disposable QEMU input image") from error

    def qemu_command(self, input_image: Path) -> list[str]:
        return [
            "qemu-system-x86_64",
            "-enable-kvm",
            "-machine",
            "microvm,accel=kvm",
            "-cpu",
            "host",
            "-smp",
            "1",
            "-m",
            "256M",
            "-nodefaults",
            "-nographic",
            "-no-reboot",
            "-nic",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(self.kernel),
            "-initrd",
            str(self.initramfs),
            "-append",
            "console=ttyS0 root=/dev/vda rootfstype=ext4 ro init=/sbin/menti-init quiet panic=1",
            "-drive",
            f"file={self.rootfs},format=raw,if=none,id=rootfs,readonly=on",
            "-device",
            "virtio-blk-device,drive=rootfs",
            "-drive",
            f"file={input_image},format=raw,if=none,id=input,readonly=on",
            "-device",
            "virtio-blk-device,drive=input",
        ]

    def _run_qemu(self, input_image: Path) -> bytes:
        command = self.qemu_command(input_image)
        environment = {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("QEMU guest exceeded the wall-time limit") from error
        except OSError as error:
            raise RuntimeError("QEMU hypervisor could not start") from error
        output = completed.stdout + b"\n" + completed.stderr
        if completed.returncode != 0 and not _RESULT_MARKER.search(output):
            raise RuntimeError("QEMU guest exited without a result")
        return output


def _asset(value: str | Path, label: str) -> Path:
    path = Path(value).resolve()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    return path


def _source_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("invalid source path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") for part in path.parts
    ):
        raise ValueError("unsafe source path")
    return path


def _write_file(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
