from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.constants import DKIM_SELECTOR, MAIL_HOST_PREFIX
from lib.dns_check import (
    DomainCheckResult,
    RecordResult,
    _build_desired_spf,
    _query_address,
    _query_txt_prefix,
    _spf_contains_ips,
    _txt_status,
    check_domain,
    print_results,
)


def _make_domain_config(
    domain: str = "example.test",
    mail: bool = True,
    mailsubdomain: bool = True,
) -> dict[str, object]:
    return {
        "domain": domain,
        "mail": mail,
        "web": True,
        "ghost": False,
        "mailsubdomain": mailsubdomain,
        "dns_management": "manual",
    }


def test_txt_status_ok() -> None:
    assert _txt_status("v=spf1 ...", "v=spf1") == "ok"


def test_txt_status_missing() -> None:
    assert _txt_status(None, "v=spf1") == "missing"


def test_txt_status_mismatch() -> None:
    assert _txt_status("v=DKIM1; ...", "v=spf1") == "mismatch"


def test_spf_contains_ips_true() -> None:
    spf = "v=spf1 include:_spf.google.com ip4:1.2.3.4 ip6:::1 -all"
    assert _spf_contains_ips(spf, "1.2.3.4", "::1") is True


def test_spf_contains_ips_missing_ipv4() -> None:
    spf = "v=spf1 include:_spf.google.com ip6:::1 -all"
    assert _spf_contains_ips(spf, "1.2.3.4", "::1") is False


def test_spf_contains_ips_missing_ipv6() -> None:
    spf = "v=spf1 ip4:1.2.3.4 -all"
    assert _spf_contains_ips(spf, "1.2.3.4", "::1") is False


def test_spf_contains_ips_no_ipv6_required() -> None:
    spf = "v=spf1 ip4:1.2.3.4 -all"
    assert _spf_contains_ips(spf, "1.2.3.4", "") is True


def test_spf_check_verifies_vps_ips_present() -> None:
    import dns.resolver

    config = _make_domain_config()

    rdata_with_google_only = MagicMock()
    rdata_with_google_only.strings = [b"v=spf1 include:_spf.google.com ~all"]

    def mock_resolve(name: str, rtype: str) -> MagicMock:
        if rtype == "TXT" and "spf" not in name and name == "example.test":
            answers = MagicMock()
            answers.__iter__ = MagicMock(return_value=iter([rdata_with_google_only]))
            return answers
        raise dns.resolver.NXDOMAIN

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=mock_resolve):
        result = check_domain(config, "1.2.3.4", "::1")

    spf_records = [
        r for r in result.records if r.record_type == "TXT" and r.name == "example.test."
    ]
    assert len(spf_records) == 1
    assert spf_records[0].status == "mismatch"


def test_query_address_returns_none_on_nxdomain() -> None:
    import dns.resolver

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
        result = _query_address("nonexistent.example.test", "A")
    assert result is None


def test_query_address_returns_none_on_no_answer() -> None:
    import dns.resolver

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=dns.resolver.NoAnswer):
        result = _query_address("example.test", "AAAA")
    assert result is None


def test_query_address_returns_ip_on_success() -> None:
    mock_rdata = MagicMock()
    mock_rdata.__str__ = MagicMock(return_value="1.2.3.4")  # type: ignore[method-assign]
    mock_answers = MagicMock()
    mock_answers.__getitem__ = MagicMock(return_value=mock_rdata)

    with patch("lib.dns_check.dns.resolver.resolve", return_value=mock_answers):
        result = _query_address("example.test", "A")
    assert result == "1.2.3.4"


def test_query_txt_prefix_returns_none_on_nxdomain() -> None:
    import dns.resolver

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
        result = _query_txt_prefix("_dmarc.example.test", "v=DMARC1")
    assert result is None


def test_query_txt_prefix_returns_matching_record() -> None:
    rdata = MagicMock()
    rdata.strings = [b"v=DMARC1; p=reject"]
    answers = MagicMock()
    answers.__iter__ = MagicMock(return_value=iter([rdata]))

    with patch("lib.dns_check.dns.resolver.resolve", return_value=answers):
        result = _query_txt_prefix("_dmarc.example.test", "v=DMARC1")
    assert result == "v=DMARC1; p=reject"


def test_check_domain_uses_wolfmail_prefix_for_a_records() -> None:
    import dns.resolver

    config = _make_domain_config(mail=False)
    expected_host = f"{MAIL_HOST_PREFIX}.example.test"

    queried: list[str] = []

    def mock_resolve(name: str, rtype: str) -> MagicMock:
        queried.append(name)
        raise dns.resolver.NXDOMAIN

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=mock_resolve):
        check_domain(config, "1.2.3.4", "::1")

    assert expected_host in queried
    assert "mail.example.test" not in queried


def test_check_domain_uses_wolfmail_selector_for_dkim() -> None:
    import dns.resolver

    config = _make_domain_config()
    expected_dkim_name = f"{DKIM_SELECTOR}._domainkey.example.test"

    queried: list[str] = []

    def mock_resolve(name: str, rtype: str) -> MagicMock:
        queried.append(name)
        raise dns.resolver.NXDOMAIN

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=mock_resolve):
        check_domain(config, "1.2.3.4", "::1")

    assert expected_dkim_name in queried
    assert "mail._domainkey.example.test" not in queried


def test_check_domain_missing_status_on_nxdomain() -> None:
    import dns.resolver

    config = _make_domain_config()

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
        result = check_domain(config, "1.2.3.4", "::1")

    assert all(r.status == "missing" for r in result.records)


def test_mismatch_status_on_wrong_ip() -> None:
    config = _make_domain_config(mail=False)
    mock_rdata = MagicMock()
    mock_rdata.__str__ = MagicMock(return_value="9.9.9.9")  # type: ignore[method-assign]
    mock_answers = MagicMock()
    mock_answers.__getitem__ = MagicMock(return_value=mock_rdata)

    with patch("lib.dns_check.dns.resolver.resolve", return_value=mock_answers):
        result = check_domain(config, "1.2.3.4", "::1")

    a_records = [r for r in result.records if r.record_type == "A"]
    assert a_records[0].status == "mismatch"


def test_result_serialises_to_dict() -> None:
    result = DomainCheckResult(
        domain="example.test",
        records=[
            RecordResult(
                domain="example.test",
                record_type="A",
                name="example.test.",
                expected="1.2.3.4",
                actual="1.2.3.4",
                status="ok",
            )
        ],
    )
    d = result.to_dict()
    assert json.dumps(d)
    assert d["domain"] == "example.test"
    assert d["records"][0]["status"] == "ok"  # type: ignore[index]


def test_copy_paste_output_has_trailing_dots(capsys: pytest.CaptureFixture[str]) -> None:
    result = DomainCheckResult(
        domain="example.test",
        records=[
            RecordResult(
                domain="example.test",
                record_type="A",
                name=f"{MAIL_HOST_PREFIX}.example.test.",
                expected="1.2.3.4",
                actual=None,
                status="missing",
            )
        ],
    )
    print_results([result])
    captured = capsys.readouterr()
    assert f"{MAIL_HOST_PREFIX}.example.test." in captured.out


def test_warning_logged_on_dns_exception(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    import dns.exception

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "lib.dns_check.dns.resolver.resolve",
            side_effect=dns.exception.DNSException("timeout"),  # type: ignore[no-untyped-call]
        ),
    ):
        result = _query_address("example.test", "A")

    assert result is None
    assert "DNS query failed" in caplog.text


def test_build_desired_spf_merges_existing() -> None:
    existing = "v=spf1 include:_spf.google.com ~all"
    rdata = MagicMock()
    rdata.strings = [existing.encode()]
    answers = MagicMock()
    answers.__iter__ = MagicMock(return_value=iter([rdata]))

    with patch("lib.dns_check.dns.resolver.resolve", return_value=answers):
        desired = _build_desired_spf("example.test", "1.2.3.4", "::1")

    assert "include:_spf.google.com" in desired
    assert "ip4:1.2.3.4" in desired
    assert "ip6:::1" in desired
    assert desired.endswith("-all")
    assert "~all" not in desired


def test_build_desired_spf_from_scratch() -> None:
    import dns.resolver

    with patch("lib.dns_check.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
        desired = _build_desired_spf("example.test", "1.2.3.4", "::1")

    assert desired.startswith("v=spf1")
    assert "ip4:1.2.3.4" in desired
    assert "ip6:::1" in desired
    assert desired.endswith("-all")
