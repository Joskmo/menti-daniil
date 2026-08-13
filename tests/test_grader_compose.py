from pathlib import Path


def test_grader_control_services_keep_suite_vault_out_of_telegram_bot() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert "menti-authoring-worker:" in compose
    assert "menti-grading-worker:" in compose
    assert "menti-check-publisher:" in compose
    assert "menti-grader-bot:" in compose
    author = compose.split("  menti-authoring-worker:", 1)[1].split(
        "  menti-grading-worker:", 1
    )[0]
    worker = compose.split("  menti-grading-worker:", 1)[1].split(
        "  menti-check-publisher:", 1
    )[0]
    publisher = compose.split("  menti-check-publisher:", 1)[1].split(
        "  menti-grader-bot:", 1
    )[0]
    bot = compose.split("  menti-grader-bot:", 1)[1].split("networks:", 1)[0]
    assert ":/suite-vault" in author
    assert ":/suite-vault:ro" not in author
    assert ":/run/menti-broker:ro" in author
    assert ":/run/menti-launcher" in author
    assert ":/suite-vault" in worker
    assert ":/git-cache" in worker
    assert ":/run/menti-launcher" in worker
    assert "github_app_private_key" not in worker
    assert ":/grader-state" in publisher
    assert "github_app_private_key" in publisher
    assert "/suite-vault" not in publisher
    assert ":/grader-state" in bot
    assert ":/menti-data" in bot
    assert "/suite-vault" not in bot
    assert "/run/menti-launcher" not in bot
    for control_service in (author, worker, publisher, bot):
        assert "healthcheck:\n      disable: true" in control_service


def test_guest_work_parent_allows_traversal_but_not_listing_for_student() -> None:
    init = Path("grader/guest/menti-init").read_text(encoding="utf-8")

    assert "size=96m,mode=0711" in init


def test_grader_guest_mountpoints_are_built_into_read_only_rootfs() -> None:
    dockerfile = Path("grader-launcher.Dockerfile").read_text(encoding="utf-8")

    assert "mkdir -p /opt/menti /guest-assets /input /work" in dockerfile


def test_grader_launcher_tmpfs_is_private_and_owned_by_unprivileged_launcher() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert "/tmp:size=64m,mode=0700,uid=1000,gid=1000" in compose
