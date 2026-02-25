from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.constants import DKIM_SELECTOR
from scripts.generate_dkim import generate_keypair, get_public_key_b64, key_needs_rotation


def test_idempotent_within_cryptoperiod(tmp_path: Path) -> None:
    domain = "example.test"
    dkim_dir = tmp_path / domain
    dkim_dir.mkdir()
    private_key = dkim_dir / "private.key"
    private_key.write_bytes(b"fake key")

    with patch("scripts.generate_dkim.EXIM_DKIM", tmp_path):
        result = generate_keypair(domain, dry_run=False, force=False)

    assert result is False


def test_rotates_when_past_cryptoperiod(tmp_path: Path) -> None:
    domain = "example.test"
    dkim_dir = tmp_path / domain
    dkim_dir.mkdir()
    private_key = dkim_dir / "private.key"
    private_key.write_bytes(b"old key")

    import os

    old_mtime = time.time() - (50 * 86400)
    os.utime(private_key, (old_mtime, old_mtime))

    result = key_needs_rotation(private_key)
    assert result is True


def test_force_flag_regenerates_regardless_of_age(tmp_path: Path) -> None:
    domain = "example.test"
    dkim_dir = tmp_path / domain
    dkim_dir.mkdir()
    private_key = dkim_dir / "private.key"
    private_key.write_bytes(b"existing key")

    with (
        patch("scripts.generate_dkim.EXIM_DKIM", tmp_path),
        patch("scripts.generate_dkim.os.chown"),
        patch("scripts.generate_dkim.pwd.getpwnam") as mock_pw,
        patch("scripts.generate_dkim.grp.getgrnam") as mock_gr,
    ):
        mock_pw.return_value.pw_uid = 0
        mock_gr.return_value.gr_gid = 0
        result = generate_keypair(domain, dry_run=False, force=True)

    assert result is True
    assert (tmp_path / domain / "private.key").stat().st_size > 0


def test_key_permissions_set_to_640(tmp_path: Path) -> None:
    domain = "example.test"

    with (
        patch("scripts.generate_dkim.EXIM_DKIM", tmp_path),
        patch("scripts.generate_dkim.os.chown"),
        patch("scripts.generate_dkim.pwd.getpwnam") as mock_pw,
        patch("scripts.generate_dkim.grp.getgrnam") as mock_gr,
    ):
        mock_pw.return_value.pw_uid = 0
        mock_gr.return_value.gr_gid = 0
        generate_keypair(domain, dry_run=False, force=False)

    private_key = tmp_path / domain / "private.key"
    assert private_key.exists()
    mode = private_key.stat().st_mode & 0o777
    assert mode == 0o640


def test_dry_run_generates_nothing(tmp_path: Path) -> None:
    domain = "example.test"

    with patch("scripts.generate_dkim.EXIM_DKIM", tmp_path):
        result = generate_keypair(domain, dry_run=True, force=False)

    assert result is True
    assert not (tmp_path / domain / "private.key").exists()


def test_key_needs_rotation_when_missing(tmp_path: Path) -> None:
    private_key = tmp_path / "nonexistent.key"
    assert key_needs_rotation(private_key) is True


def test_get_public_key_b64_returns_string(tmp_path: Path) -> None:
    domain = "example.test"

    with (
        patch("scripts.generate_dkim.EXIM_DKIM", tmp_path),
        patch("scripts.generate_dkim.os.chown"),
        patch("scripts.generate_dkim.pwd.getpwnam") as mock_pw,
        patch("scripts.generate_dkim.grp.getgrnam") as mock_gr,
    ):
        mock_pw.return_value.pw_uid = 0
        mock_gr.return_value.gr_gid = 0
        generate_keypair(domain, dry_run=False, force=False)

    private_key = tmp_path / domain / "private.key"
    b64 = get_public_key_b64(private_key)
    assert isinstance(b64, str)
    assert len(b64) > 100


def test_dns_output_uses_wolfmail_selector(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    domain = "example.test"

    with (
        patch("scripts.generate_dkim.EXIM_DKIM", tmp_path),
        patch("scripts.generate_dkim.os.chown"),
        patch("scripts.generate_dkim.pwd.getpwnam") as mock_pw,
        patch("scripts.generate_dkim.grp.getgrnam") as mock_gr,
        caplog.at_level(logging.INFO),
    ):
        mock_pw.return_value.pw_uid = 0
        mock_gr.return_value.gr_gid = 0
        generate_keypair(domain, dry_run=False, force=False)

    assert f"{DKIM_SELECTOR}._domainkey.{domain}" in caplog.text
    assert " mail._domainkey" not in caplog.text  # "wolfmail" is fine; bare "mail." is not
