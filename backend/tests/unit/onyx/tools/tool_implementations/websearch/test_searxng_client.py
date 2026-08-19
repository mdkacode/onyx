"""Tests for the SearXNG web search client."""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import requests

from onyx.error_handling.exceptions import OnyxError
from onyx.tools.tool_implementations.web_search.clients.searxng_client import (
    RetryableSearXNGError,
)
from onyx.tools.tool_implementations.web_search.clients.searxng_client import (
    SearXNGClient,
)

POST_TARGET = (
    "onyx.tools.tool_implementations.web_search.clients.searxng_client.requests.post"
)


def _response(
    *, status_code: int = 200, payload: dict[str, Any] | None = None
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {"results": []}
    response.raise_for_status.return_value = None
    return response


def _page(*results: dict[str, Any]) -> MagicMock:
    return _response(payload={"results": list(results)})


def test_results_missing_optional_fields_are_still_usable() -> None:
    """SearXNG merges many engines, and several omit `content` or `title`.

    A missing key must cost us that field, never the whole search.
    """
    client = SearXNGClient("http://searxng:8080", num_results=5)

    with patch(
        POST_TARGET,
        return_value=_page(
            {"url": "https://example.com/a", "title": "A"},  # no content
            {"url": "https://example.com/b", "content": "B body"},  # no title
            {"url": "https://example.com/c", "title": "C", "content": "C body"},
        ),
    ):
        results = client.search("ev batteries")

    assert [(r.title, r.snippet) for r in results] == [
        ("A", ""),
        ("", "B body"),
        ("C", "C body"),
    ]


def test_results_without_a_url_are_skipped() -> None:
    client = SearXNGClient("http://searxng:8080", num_results=5)

    with patch(
        POST_TARGET,
        return_value=_page(
            {"title": "no link here", "content": "orphan"},
            {"url": "", "title": "empty link"},
            {"url": "https://example.com/real", "title": "real"},
        ),
    ):
        results = client.search("ev batteries")

    assert [r.link for r in results] == ["https://example.com/real"]


def test_published_date_is_parsed_and_bad_dates_are_tolerated() -> None:
    client = SearXNGClient("http://searxng:8080", num_results=5)

    with patch(
        POST_TARGET,
        return_value=_page(
            {
                "url": "https://example.com/dated",
                "title": "dated",
                "publishedDate": "2026-03-04T00:00:00+00:00",
            },
            {
                "url": "https://example.com/garbled",
                "title": "garbled",
                "publishedDate": "sometime last spring",
            },
        ),
    ):
        results = client.search("ev batteries")

    assert results[0].published_date is not None
    assert results[0].published_date.year == 2026
    assert results[1].published_date is None


def test_pagination_fills_the_requested_result_count() -> None:
    """SearXNG has no result-count parameter, so the client pages instead."""
    client = SearXNGClient("http://searxng:8080", num_results=4, max_pages=3)

    pages = [
        _page(
            {"url": "https://example.com/1", "title": "1"},
            {"url": "https://example.com/2", "title": "2"},
        ),
        _page(
            {"url": "https://example.com/3", "title": "3"},
            {"url": "https://example.com/4", "title": "4"},
        ),
    ]

    with patch(POST_TARGET, side_effect=pages) as mock_post:
        results = client.search("ev batteries")

    assert len(results) == 4
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[1].kwargs["data"]["pageno"] == "2"


def test_duplicate_links_across_pages_are_dropped() -> None:
    client = SearXNGClient("http://searxng:8080", num_results=10, max_pages=2)

    pages = [
        _page({"url": "https://example.com/a?utm_source=x", "title": "a"}),
        _page(
            {"url": "https://example.com/a", "title": "a again"},
            {"url": "https://example.com/b", "title": "b"},
        ),
    ]

    with patch(POST_TARGET, side_effect=pages):
        results = client.search("ev batteries")

    assert [r.link for r in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_paging_stops_on_an_empty_page() -> None:
    client = SearXNGClient("http://searxng:8080", num_results=10, max_pages=5)

    pages = [_page({"url": "https://example.com/1", "title": "1"}), _page()]

    with patch(POST_TARGET, side_effect=pages) as mock_post:
        results = client.search("ev batteries")

    assert len(results) == 1
    assert mock_post.call_count == 2


def test_forbidden_response_is_a_config_error_and_is_not_retried() -> None:
    """403 means JSON output is off -- retrying just delays the same failure."""
    client = SearXNGClient("http://searxng:8080")

    with patch(POST_TARGET, return_value=_response(status_code=403)) as mock_post:
        with pytest.raises(OnyxError, match="search.formats"):
            client.search("ev batteries")

    assert mock_post.call_count == 1


def test_not_found_response_is_a_config_error() -> None:
    client = SearXNGClient("http://searxng:8080")

    with patch(POST_TARGET, return_value=_response(status_code=404)):
        with pytest.raises(OnyxError, match="404"):
            client.search("ev batteries")


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
def test_transient_statuses_are_retryable(status_code: int) -> None:
    client = SearXNGClient("http://searxng:8080")

    with patch(POST_TARGET, return_value=_response(status_code=status_code)):
        with pytest.raises(RetryableSearXNGError):
            client._request_page("ev batteries", 1)  # noqa: SLF001


def test_timeout_is_retryable() -> None:
    client = SearXNGClient("http://searxng:8080", timeout_seconds=3)

    with patch(POST_TARGET, side_effect=requests.Timeout()):
        with pytest.raises(RetryableSearXNGError, match="timed out after 3s"):
            client._request_page("ev batteries", 1)  # noqa: SLF001


def test_requests_carry_a_timeout() -> None:
    """An unbounded search request would pin a worker thread indefinitely."""
    client = SearXNGClient("http://searxng:8080", timeout_seconds=7)

    with patch(POST_TARGET, return_value=_page()) as mock_post:
        client.search("ev batteries")

    assert mock_post.call_args.kwargs["timeout"] == 7


def test_trailing_slash_in_base_url_does_not_double_up() -> None:
    client = SearXNGClient("http://searxng:8080/", num_results=1)

    with patch(POST_TARGET, return_value=_page()) as mock_post:
        client.search("ev batteries")

    assert mock_post.call_args.args[0] == "http://searxng:8080/search"


def test_optional_search_parameters_are_forwarded() -> None:
    client = SearXNGClient(
        "http://searxng:8080",
        language="en",
        time_range="month",
        safesearch="1",
        categories="news",
        engines="duckduckgo,brave",
    )

    with patch(POST_TARGET, return_value=_page()) as mock_post:
        client.search("ev batteries")

    payload = mock_post.call_args.kwargs["data"]
    assert payload["language"] == "en"
    assert payload["time_range"] == "month"
    assert payload["safesearch"] == "1"
    assert payload["categories"] == "news"
    assert payload["engines"] == "duckduckgo,brave"


def test_omitted_optional_parameters_are_not_sent() -> None:
    client = SearXNGClient("http://searxng:8080")

    with patch(POST_TARGET, return_value=_page()) as mock_post:
        client.search("ev batteries")

    payload = mock_post.call_args.kwargs["data"]
    assert set(payload) == {"q", "format", "pageno"}


def test_invalid_time_range_is_rejected_at_construction() -> None:
    """SearXNG silently ignores an unknown time_range, so catch it here instead."""
    with pytest.raises(OnyxError, match="time_range"):
        SearXNGClient("http://searxng:8080", time_range="fortnight")


def test_invalid_safesearch_is_rejected_at_construction() -> None:
    with pytest.raises(OnyxError, match="safesearch"):
        SearXNGClient("http://searxng:8080", safesearch="9")
