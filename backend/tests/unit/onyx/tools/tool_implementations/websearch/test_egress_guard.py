"""Tests for the one-way egress guard on outbound web-search queries."""

import pytest
from pytest import MonkeyPatch

from onyx.tools.tool_implementations.web_search import egress_guard
from onyx.tools.tool_implementations.web_search.egress_guard import (
    guard_outbound_queries,
)


@pytest.fixture(autouse=True)
def _guard_enabled(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(egress_guard, "WEB_SEARCH_EGRESS_GUARD_ENABLED", True)
    monkeypatch.setattr(egress_guard, "WEB_SEARCH_MAX_QUERY_CHARS", 256)
    monkeypatch.setattr(egress_guard, "WEB_SEARCH_MAX_QUOTED_WORDS", 12)
    monkeypatch.setattr(egress_guard, "WEB_SEARCH_INTERNAL_DOMAINS", [])


def test_ordinary_query_passes_through_untouched() -> None:
    (verdict,) = guard_outbound_queries(["lithium iron phosphate bus battery life"])

    assert verdict.sanitized == "lithium iron phosphate bus battery life"
    assert verdict.block_reason is None
    assert not verdict.was_modified


def test_overlong_query_is_blocked() -> None:
    pasted_document_text = "internal quarterly figures " * 40

    (verdict,) = guard_outbound_queries([pasted_document_text])

    assert verdict.sanitized is None
    assert "over the 256-character limit" in (verdict.block_reason or "")


# These are synthetic credentials in the shapes the guard matches on. They have
# to look real to exercise it, hence the allowlist pragmas.
@pytest.mark.parametrize(
    "query",
    [
        "AKIAIOSFODNN7EXAMPLE s3 error",
        "why does sk-abcdefghijklmnopqrstuvwxyz012345 fail",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789 rate limit",  # pragma: allowlist secret
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",  # pragma: allowlist secret
        "xoxb-1234567890-abcdefghijkl slack api",  # pragma: allowlist secret
        "Authorization bearer abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_credentials_are_blocked(query: str) -> None:
    (verdict,) = guard_outbound_queries([query])

    assert verdict.sanitized is None
    assert "credential" in (verdict.block_reason or "")


def test_long_quoted_passage_is_blocked() -> None:
    query = (
        '"the fleet maintenance contract shall be renewed annually subject to '
        'written approval by both parties"'
    )

    (verdict,) = guard_outbound_queries([query])

    assert verdict.sanitized is None
    assert "verbatim passage" in (verdict.block_reason or "")


def test_short_quoted_phrase_is_allowed() -> None:
    (verdict,) = guard_outbound_queries(['"battery thermal runaway" standards'])

    assert verdict.sanitized == '"battery thermal runaway" standards'


def test_internal_hostnames_are_stripped(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        egress_guard, "WEB_SEARCH_INTERNAL_DOMAINS", ["gyan.naarni.com", "naarni.com"]
    )

    (verdict,) = guard_outbound_queries(
        ["opensearch timeout on gyan.naarni.com/admin ask ops@naarni.com"]
    )

    assert verdict.sanitized == "opensearch timeout on ask"
    assert "internal_hostname" in verdict.redactions


def test_private_ips_and_long_digit_runs_are_stripped() -> None:
    (verdict,) = guard_outbound_queries(
        ["connection refused 172.16.0.10 account 4111 1111 1111 1111"]
    )

    assert verdict.sanitized == "connection refused account"
    assert "private_ip" in verdict.redactions
    assert "long_digit_run" in verdict.redactions


def test_local_paths_are_stripped() -> None:
    (verdict,) = guard_outbound_queries(["permission denied /var/log/onyx/api.log"])

    assert verdict.sanitized == "permission denied"
    assert "local_path" in verdict.redactions


def test_query_that_is_entirely_internal_is_blocked(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(egress_guard, "WEB_SEARCH_INTERNAL_DOMAINS", ["naarni.com"])

    (verdict,) = guard_outbound_queries(["gyan.naarni.com"])

    assert verdict.sanitized is None
    assert "nothing searchable remained" in (verdict.block_reason or "")


def test_verdicts_are_returned_in_input_order() -> None:
    verdicts = guard_outbound_queries(
        ["ev charging standards", "AKIAIOSFODNN7EXAMPLE", "battery recycling india"]
    )

    assert [v.sanitized for v in verdicts] == [
        "ev charging standards",
        None,
        "battery recycling india",
    ]


def test_disabled_guard_passes_everything(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(egress_guard, "WEB_SEARCH_EGRESS_GUARD_ENABLED", False)

    (verdict,) = guard_outbound_queries(["AKIAIOSFODNN7EXAMPLE"])

    assert verdict.sanitized == "AKIAIOSFODNN7EXAMPLE"
