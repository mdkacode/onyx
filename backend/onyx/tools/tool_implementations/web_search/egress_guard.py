"""One-way egress guard for outbound web-search queries.

Onyx's web search is deliberately one-directional: the public internet is a
source of information, never a destination for ours. On that path exactly one
thing leaves the network -- the search query string -- and it is written by an
LLM whose context is full of internal documents. So the query is the single
place where internal content can escape, and this module is the chokepoint that
sits in front of it.

Every query passes through here before it reaches *any* search provider (not
just SearXNG), so the boundary does not depend on which provider is configured.

Two kinds of finding:

* **Block** -- material that must never leave under any circumstance
  (credentials, key material) or that signals the LLM is pasting internal text
  rather than composing a query (very long queries, long quoted spans). The
  query is dropped and the LLM is told to rewrite it.
* **Redact** -- material that is plausibly incidental (internal hostnames,
  account-number-shaped digit runs, local filesystem paths). The token is
  removed and the rest of the query still goes out.

Blocking long queries is a search-quality win as much as a privacy one: search
engines return noise for 500-character strings, so forcing a short keyword
query makes the answer better, not worse.
"""

import re
from dataclasses import dataclass
from dataclasses import field

from onyx.configs.app_configs import WEB_SEARCH_EGRESS_GUARD_ENABLED
from onyx.configs.app_configs import WEB_SEARCH_INTERNAL_DOMAINS
from onyx.configs.app_configs import WEB_SEARCH_MAX_QUERY_CHARS
from onyx.configs.app_configs import WEB_SEARCH_MAX_QUOTED_WORDS
from onyx.utils.logger import setup_logger

logger = setup_logger()

# If redaction leaves less than this behind, the query was essentially all
# internal identifiers; sending the remainder would only return noise.
MIN_USEFUL_QUERY_CHARS = 3


# Credentials and key material. These never leave, and there is no plausible
# reason for a legitimate web search to contain one.
_BLOCK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    ("provider_api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    (
        "azure_storage_key",
        re.compile(r"(?i)\b(?:AccountKey|SharedAccessSignature)\s*="),
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    # 40+ unbroken base64/hex characters is not something a person searches for;
    # it is a key, a hash of something internal, or a dumped blob.
    ("opaque_blob", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
]

# Removed from the query, which is then still sent.
_REDACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Account numbers, card numbers, Aadhaar, long internal record IDs.
    ("long_digit_run", re.compile(r"\b\d[\d\s-]{10,}\d\b")),
    # RFC1918 / link-local addresses -- internal topology.
    (
        "private_ip",
        re.compile(
            r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|169\.254)"
            r"\.\d{1,3}(?:\.\d{1,3})?\b"
        ),
    ),
    # Local filesystem paths, POSIX and Windows.
    (
        "local_path",
        re.compile(r"(?:/(?:home|Users|opt|var|etc|srv|mnt|root)/\S+|[A-Za-z]:\\\S+)"),
    ),
]

_QUOTED_SPAN = re.compile(r"\"([^\"]+)\"")


@dataclass
class QueryVerdict:
    """What the guard decided about one outbound query."""

    original: str
    # None means the query was blocked and must not be sent.
    sanitized: str | None
    block_reason: str | None = None
    redactions: list[str] = field(default_factory=list)

    @property
    def was_modified(self) -> bool:
        return self.sanitized != self.original


def _internal_host_pattern() -> re.Pattern[str] | None:
    """Build a matcher for this deployment's own hostnames.

    Anything naming our own infrastructure (`gyan.naarni.com`, an internal
    wiki host, a Kubernetes service name) is internal topology and is stripped
    before the query leaves.
    """
    hosts = {h.strip().lower() for h in WEB_SEARCH_INTERNAL_DOMAINS if h.strip()}
    if not hosts:
        return None
    # Longest host first so "gyan.naarni.com" is matched before "naarni.com".
    ordered = sorted(hosts, key=lambda host: len(host), reverse=True)
    alternation = "|".join(re.escape(host) for host in ordered)
    # Match the host itself plus any subdomain / path / email local-part that
    # carries it, e.g. "wiki.example.internal/page" or "ops@example.internal".
    return re.compile(rf"\b[\w.@+-]*(?:{alternation})\S*", re.IGNORECASE)


def _guard_one(query: str, internal_pattern: re.Pattern[str] | None) -> QueryVerdict:
    # Length first: an over-long query is the strongest single signal that the
    # model pasted document text instead of composing a search.
    if len(query) > WEB_SEARCH_MAX_QUERY_CHARS:
        return QueryVerdict(
            original=query,
            sanitized=None,
            block_reason=(
                f"query is {len(query)} characters, over the "
                f"{WEB_SEARCH_MAX_QUERY_CHARS}-character limit for outbound queries"
            ),
        )

    for name, pattern in _BLOCK_PATTERNS:
        if pattern.search(query):
            return QueryVerdict(
                original=query,
                sanitized=None,
                block_reason=f"query appears to contain a credential ({name})",
            )

    # An exact-phrase search over a long verbatim span is how internal prose
    # leaks a sentence at a time.
    for span in _QUOTED_SPAN.findall(query):
        word_count = len(span.split())
        if word_count > WEB_SEARCH_MAX_QUOTED_WORDS:
            return QueryVerdict(
                original=query,
                sanitized=None,
                block_reason=(
                    f"query quotes a {word_count}-word verbatim passage, over the "
                    f"{WEB_SEARCH_MAX_QUOTED_WORDS}-word limit for exact-phrase search"
                ),
            )

    sanitized = query
    redactions: list[str] = []

    if internal_pattern is not None:
        sanitized, replaced = internal_pattern.subn(" ", sanitized)
        if replaced:
            redactions.append("internal_hostname")

    for name, pattern in _REDACT_PATTERNS:
        sanitized, replaced = pattern.subn(" ", sanitized)
        if replaced:
            redactions.append(name)

    sanitized = " ".join(sanitized.split())

    # Only a query that redaction *emptied* is blocked here. A query the model
    # deliberately wrote short ("EV", "Go") is its own call to make and carries
    # nothing internal, so it passes.
    if redactions and len(sanitized) < MIN_USEFUL_QUERY_CHARS:
        return QueryVerdict(
            original=query,
            sanitized=None,
            block_reason="nothing searchable remained after removing internal identifiers",
            redactions=redactions,
        )

    return QueryVerdict(original=query, sanitized=sanitized, redactions=redactions)


def guard_outbound_queries(queries: list[str]) -> list[QueryVerdict]:
    """Screen every query about to leave for the public internet.

    Returns one verdict per input query, in order. Callers must send only
    `verdict.sanitized` values that are not None.
    """
    if not WEB_SEARCH_EGRESS_GUARD_ENABLED:
        return [QueryVerdict(original=q, sanitized=q) for q in queries]

    internal_pattern = _internal_host_pattern()
    verdicts = [_guard_one(q, internal_pattern) for q in queries]

    # Audit trail. The sanitized query is safe to log; the original is not
    # (it is the thing that may hold a credential), so only its shape is
    # recorded alongside the rule that fired.
    for verdict in verdicts:
        if verdict.sanitized is None:
            logger.warning(
                "Web search egress guard BLOCKED a query (%d chars): %s",
                len(verdict.original),
                verdict.block_reason,
            )
        elif verdict.redactions:
            logger.info(
                "Web search egress guard redacted %s from a query; sending: %s",
                ", ".join(sorted(set(verdict.redactions))),
                verdict.sanitized,
            )

    return verdicts
