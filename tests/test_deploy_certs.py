from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.constants import MAIL_HOST_PREFIX
from scripts.deploy_certs import cert_changed, deploy_domain, install_cert


def test_cert_changed_when_dst_missing(tmp_path: Path) -> None:
    src = tmp_path / "cert.pem"
    src.write_bytes(b"cert data")
    dst = tmp_path / "nonexistent.pem"
    assert cert_changed(src, dst) is True


def test_cert_changed_when_identical(tmp_path: Path) -> None:
    data = b"identical cert data"
    src = tmp_path / "src.pem"
    dst = tmp_path / "dst.pem"
    src.write_bytes(data)
    dst.write_bytes(data)
    assert cert_changed(src, dst) is False


def test_cert_changed_when_different(tmp_path: Path) -> None:
    src = tmp_path / "src.pem"
    dst = tmp_path / "dst.pem"
    src.write_bytes(b"new cert")
    dst.write_bytes(b"old cert")
    assert cert_changed(src, dst) is True


def test_install_cert_dry_run(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    src = tmp_path / "src.pem"
    dst = tmp_path / "dst.pem"
    src.write_bytes(b"cert")
    with caplog.at_level(logging.INFO):
        install_cert(src, dst, dry_run=True)
    assert not dst.exists()
    assert "dry-run" in caplog.text


def test_deploy_domain_uses_mail_host_prefix_constant(tmp_path: Path) -> None:
    mail_host = f"{MAIL_HOST_PREFIX}.other.example.test"
    src_dir = tmp_path / "caddy" / mail_host
    src_dir.mkdir(parents=True)
    (src_dir / f"{mail_host}.crt").write_bytes(b"cert")
    (src_dir / f"{mail_host}.key").write_bytes(b"key")

    with (
        patch("scripts.deploy_certs.EXIM_CERTS", tmp_path / "exim"),
        patch("scripts.deploy_certs.install_cert") as mock_install,
    ):
        result = deploy_domain(
            "other.example.test",
            mail_host,
            tmp_path / "caddy",
            dry_run=False,
            force=False,
        )

    assert result is True
    assert mock_install.call_count == 2
    assert MAIL_HOST_PREFIX in mail_host


def test_deploy_domain_calls_install_when_changed(tmp_path: Path) -> None:
    mail_host = f"{MAIL_HOST_PREFIX}.other.example.test"
    src_dir = tmp_path / "caddy" / mail_host
    src_dir.mkdir(parents=True)
    (src_dir / f"{mail_host}.crt").write_bytes(b"new cert")
    (src_dir / f"{mail_host}.key").write_bytes(b"new key")

    with (
        patch("scripts.deploy_certs.EXIM_CERTS", tmp_path / "exim"),
        patch("scripts.deploy_certs.install_cert") as mock_install,
    ):
        result = deploy_domain(
            "other.example.test",
            mail_host,
            tmp_path / "caddy",
            dry_run=False,
            force=False,
        )

    assert result is True
    assert mock_install.call_count == 2


def test_deploy_domain_skips_when_unchanged(tmp_path: Path) -> None:
    data = b"same data"
    mail_host = f"{MAIL_HOST_PREFIX}.other.example.test"
    src_dir = tmp_path / "caddy" / mail_host
    src_dir.mkdir(parents=True)
    (src_dir / f"{mail_host}.crt").write_bytes(data)
    (src_dir / f"{mail_host}.key").write_bytes(data)

    dst_dir = tmp_path / "exim" / "other.example.test"
    dst_dir.mkdir(parents=True)
    (dst_dir / "cert.pem").write_bytes(data)
    (dst_dir / "key.pem").write_bytes(data)

    with (
        patch("scripts.deploy_certs.EXIM_CERTS", tmp_path / "exim"),
        patch("scripts.deploy_certs.install_cert") as mock_install,
    ):
        result = deploy_domain(
            "other.example.test",
            mail_host,
            tmp_path / "caddy",
            dry_run=False,
            force=False,
        )

    assert result is False
    mock_install.assert_not_called()


def test_deploy_domain_warns_on_missing_src(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    mail_host = f"{MAIL_HOST_PREFIX}.other.example.test"
    src_dir = tmp_path / "caddy" / mail_host
    src_dir.mkdir(parents=True)

    with (
        patch("scripts.deploy_certs.EXIM_CERTS", tmp_path / "exim"),
        caplog.at_level(logging.WARNING),
    ):
        result = deploy_domain(
            "other.example.test",
            mail_host,
            tmp_path / "caddy",
            dry_run=False,
            force=False,
        )

    assert result is False
    assert "not found" in caplog.text


def test_deploy_domain_force_flag(tmp_path: Path) -> None:
    data = b"same data"
    mail_host = f"{MAIL_HOST_PREFIX}.other.example.test"
    src_dir = tmp_path / "caddy" / mail_host
    src_dir.mkdir(parents=True)
    (src_dir / f"{mail_host}.crt").write_bytes(data)
    (src_dir / f"{mail_host}.key").write_bytes(data)

    dst_dir = tmp_path / "exim" / "other.example.test"
    dst_dir.mkdir(parents=True)
    (dst_dir / "cert.pem").write_bytes(data)
    (dst_dir / "key.pem").write_bytes(data)

    with (
        patch("scripts.deploy_certs.EXIM_CERTS", tmp_path / "exim"),
        patch("scripts.deploy_certs.install_cert") as mock_install,
    ):
        result = deploy_domain(
            "other.example.test",
            mail_host,
            tmp_path / "caddy",
            dry_run=False,
            force=True,
        )

    assert result is True
    assert mock_install.call_count == 2


def test_dry_run_makes_no_filesystem_changes(tmp_path: Path) -> None:
    mail_host = f"{MAIL_HOST_PREFIX}.other.example.test"
    src_dir = tmp_path / "caddy" / mail_host
    src_dir.mkdir(parents=True)
    (src_dir / f"{mail_host}.crt").write_bytes(b"cert")
    (src_dir / f"{mail_host}.key").write_bytes(b"key")

    with patch("scripts.deploy_certs.EXIM_CERTS", tmp_path / "exim"):
        result = deploy_domain(
            "other.example.test",
            mail_host,
            tmp_path / "caddy",
            dry_run=True,
            force=False,
        )

    assert result is True
    assert not (tmp_path / "exim" / "other.example.test" / "cert.pem").exists()


def test_mailsubdomain_derives_wolfmail_host(tmp_path: Path) -> None:
    mail_host = f"{MAIL_HOST_PREFIX}.other.example.test"
    src_dir = tmp_path / "caddy" / mail_host
    src_dir.mkdir(parents=True)
    (src_dir / f"{mail_host}.crt").write_bytes(b"cert")
    (src_dir / f"{mail_host}.key").write_bytes(b"key")

    with (
        patch("scripts.deploy_certs.EXIM_CERTS", tmp_path / "exim"),
        patch("scripts.deploy_certs.install_cert") as mock_install,
    ):
        result = deploy_domain(
            "other.example.test",
            mail_host,
            tmp_path / "caddy",
            dry_run=False,
            force=False,
        )

    assert result is True
    call_args = [str(c.args[0]) for c in mock_install.call_args_list]
    assert any(MAIL_HOST_PREFIX in arg for arg in call_args)
