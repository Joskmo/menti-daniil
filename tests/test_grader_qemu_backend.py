import base64
import json
from pathlib import Path

import pytest

from grader.qemu_backend import QemuCaseBackend


def _execution() -> dict:
    return {
        "version": 1,
        "request_id": "a" * 32,
        "adapter": "python_call",
        "target": "main:double",
        "input": {"args": [4], "kwargs": {}, "files": []},
        "observe_files": [],
    }


def _response() -> dict:
    return {
        "version": 1,
        "request_id": "a" * 32,
        "status": "ok",
        "observation": {
            "return": 8,
            "exception": None,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "files": [],
        },
    }


def test_qemu_backend_builds_single_input_image_without_expected_values(tmp_path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("rootfs.ext4", "vmlinuz", "initramfs"):
        (assets / name).write_bytes(b"asset")
    captured = []

    def image_builder(tree: Path, image: Path) -> None:
        request = json.loads((tree / "request.json").read_text())
        source = (tree / "source" / "main.py").read_text()
        captured.append((request, source, image))
        image.write_bytes(b"image")

    response_payload = base64.b64encode(
        json.dumps(_response(), separators=(",", ":")).encode()
    ).decode()

    def hypervisor(input_image: Path) -> bytes:
        assert input_image.read_bytes() == b"image"
        return f"\x1bc\x1b[2JMENTI_RESULT:{response_payload}\r\n".encode()

    backend = QemuCaseBackend(
        rootfs=assets / "rootfs.ext4",
        kernel=assets / "vmlinuz",
        initramfs=assets / "initramfs",
        image_builder=image_builder,
        hypervisor=hypervisor,
    )

    result = backend(
        ({"path": "main.py", "content": "def double(x): return x * 2\n"},),
        _execution(),
    )

    assert result == _response()
    request, source, input_image = captured[0]
    assert request == _execution()
    assert source == "def double(x): return x * 2\n"
    assert "expect" not in json.dumps(request)
    assert not input_image.exists()


def test_microvm_command_uses_mmio_virtio_without_pci_bus(tmp_path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("rootfs.ext4", "vmlinuz", "initramfs"):
        (assets / name).write_bytes(b"asset")
    input_image = tmp_path / "input.ext4"
    input_image.write_bytes(b"input")
    backend = QemuCaseBackend(
        rootfs=assets / "rootfs.ext4",
        kernel=assets / "vmlinuz",
        initramfs=assets / "initramfs",
    )

    command = backend.qemu_command(input_image)

    assert "virtio-blk-device,drive=rootfs" in command
    assert "virtio-blk-device,drive=input" in command
    assert any("rootfstype=ext4" in argument for argument in command)
    assert not any("if=virtio" in argument for argument in command)


def test_qemu_backend_fails_closed_without_authenticated_result_marker(tmp_path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("rootfs.ext4", "vmlinuz", "initramfs"):
        (assets / name).write_bytes(b"asset")

    def image_builder(tree: Path, image: Path) -> None:
        image.write_bytes(b"image")

    backend = QemuCaseBackend(
        rootfs=assets / "rootfs.ext4",
        kernel=assets / "vmlinuz",
        initramfs=assets / "initramfs",
        image_builder=image_builder,
        hypervisor=lambda image: b"student-controlled-noise",
    )

    with pytest.raises(RuntimeError, match="valid result"):
        backend(({"path": "main.py", "content": "pass\n"},), _execution())
