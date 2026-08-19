"""SearXNG web search client.

SearXNG is a self-hosted metasearch engine: it forwards a query to public search
engines and merges the results. Onyx talks to a private instance, which makes it
the only search provider that involves no third-party API account -- the query
leaves our network only as an anonymous request from our own relay, and no
vendor accumulates a profile of what the company searches for.

See deployment/searxng/ for the provisioning of that relay.
"""

from datetime import datetime
from typing import Any

import requests

from onyx.connectors.cross_connector_utils.miscellaneous_utils import time_str_to_utc
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.tools.tool_implementations.web_search.models import WebSearchProvider
from onyx.tools.tool_implementations.web_search.models import WebSearchResult
from onyx.utils.logger import setup_logger
from onyx.utils.retry_wrapper import retry_builder

logger = setup_logger()

DEFAULT_TIMEOUT_SECONDS = 15
# SearXNG merges engine results into pages of roughly 10-20. Onyx asks for up to
# 20, so allow a couple of extra pages to actually fill that -- but no more: each
# page is a fresh fan-out to every upstream engine and gets slower and less
# relevant as it goes.
DEFAULT_MAX_PAGES = 3
MAX_PAGES_CEILING = 10

# Values SearXNG accepts for `time_range`. Anything else is a silent no-op there,
# so reject it at config time instead of letting it quietly do nothing.
VALID_TIME_RANGES = {"day", "week", "month", "year"}
VALID_SAFESEARCH = {"0", "1", "2"}


class RetryableSearXNGError(Exception):
    """Transient SearXNG failure worth retrying.

    Deliberately narrow: a 403 (JSON format not enabled) or 404 (wrong URL) is a
    misconfiguration that will fail identically three times in a row, so retrying
    it only makes the user wait longer for the same error.
    """


def _config_error(detail: str) -> OnyxError:
    return OnyxError(OnyxErrorCode.INVALID_INPUT, detail)


class SearXNGClient(WebSearchProvider):
    def __init__(
        self,
        searxng_base_url: str,
        num_results: int = 10,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        language: str | None = None,
        time_range: str | None = None,
        safesearch: str | None = None,
        categories: str | None = None,
        engines: str | None = None,
    ) -> None:
        # A trailing slash would produce `.../` + `/search`, which some reverse
        # proxies in front of SearXNG answer with a 404.
        self._base_url = searxng_base_url.rstrip("/")
        self._num_results = max(1, num_results)
        self._timeout_seconds = timeout_seconds
        self._max_pages = max(1, min(max_pages, MAX_PAGES_CEILING))

        self._language = language or None
        self._categories = categories or None
        self._engines = engines or None

        if time_range and time_range not in VALID_TIME_RANGES:
            raise _config_error(
                f"SearXNG 'time_range' must be one of "
                f"{sorted(VALID_TIME_RANGES)}, got '{time_range}'."
            )
        self._time_range = time_range or None

        if safesearch and safesearch not in VALID_SAFESEARCH:
            raise _config_error(
                f"SearXNG 'safesearch' must be one of "
                f"{sorted(VALID_SAFESEARCH)} (0=off, 1=moderate, 2=strict), "
                f"got '{safesearch}'."
            )
        self._safesearch = safesearch or None

        logger.debug(
            "Initialized SearXNGClient base_url=%s num_results=%d max_pages=%d",
            self._base_url,
            self._num_results,
            self._max_pages,
        )

    # ------------------------------------------------------------------ search

    def _build_payload(self, query: str, page: int) -> dict[str, str]:
        payload: dict[str, str] = {
            "q": query,
            "format": "json",
            "pageno": str(page),
        }
        if self._language:
            payload["language"] = self._language
        if self._time_range:
            payload["time_range"] = self._time_range
        if self._safesearch:
            payload["safesearch"] = self._safesearch
        if self._categories:
            payload["categories"] = self._categories
        if self._engines:
            payload["engines"] = self._engines
        return payload

    def _request_page(self, query: str, page: int) -> list[dict[str, Any]]:
        try:
            response = requests.post(
                f"{self._base_url}/search",
                data=self._build_payload(query, page),
                headers={"Accept": "application/json"},
                timeout=self._timeout_seconds,
            )
        except requests.Timeout as e:
            raise RetryableSearXNGError(
                f"SearXNG timed out after {self._timeout_seconds}s"
            ) from e
        except requests.RequestException as e:
            raise RetryableSearXNGError(f"SearXNG request failed: {e}") from e

        if response.status_code == 403:
            # Config problem, not a flake: JSON output is off in settings.yml.
            raise _config_error(
                "SearXNG returned 403. Enable JSON output by adding 'json' to "
                "the 'search.formats' list in your SearXNG settings.yml."
            )
        if response.status_code == 404:
            raise _config_error(
                f"SearXNG returned 404 for {self._base_url}/search. "
                "Check that the base URL points at a SearXNG instance."
            )
        if response.status_code >= 500 or response.status_code == 429:
            raise RetryableSearXNGError(
                f"SearXNG returned {response.status_code}; retrying"
            )
        response.raise_for_status()

        try:
            body = response.json()
        except ValueError as e:
            raise RetryableSearXNGError(
                "SearXNG returned a non-JSON body; the instance may be serving "
                "an error page"
            ) from e

        results = body.get("results")
        return results if isinstance(results, list) else []

    @staticmethod
    def _parse_published_date(raw: Any) -> datetime | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return time_str_to_utc(raw)
        except Exception:
            # Engines report dates in whatever format they please; an unparseable
            # one should cost us the date, not the result.
            return None

    @staticmethod
    def _to_result(raw: dict[str, Any]) -> WebSearchResult | None:
        """Convert one SearXNG result, or None if it is unusable.

        SearXNG results are the union of many engines' schemas, so fields are
        genuinely optional -- image and video engines routinely return no
        `content`, and a missing key here used to abort the entire search.
        """
        link = raw.get("url")
        if not isinstance(link, str) or not link.strip():
            return None

        return WebSearchResult(
            title=(raw.get("title") or "").strip(),
            link=link.strip(),
            snippet=(raw.get("content") or "").strip(),
            published_date=SearXNGClient._parse_published_date(
                raw.get("publishedDate")
            ),
        )

    @retry_builder(tries=3, delay=1, backoff=2, exceptions=(RetryableSearXNGError,))
    def search(self, query: str) -> list[WebSearchResult]:
        results: list[WebSearchResult] = []
        seen_links: set[str] = set()

        # SearXNG has no result-count parameter, so page until we have enough.
        for page in range(1, self._max_pages + 1):
            raw_results = self._request_page(query, page)
            if not raw_results:
                break

            for raw in raw_results:
                result = self._to_result(raw)
                if result is None:
                    continue
                # The same page surfaces from several engines and across pages;
                # duplicates waste the LLM's context. `link` is already
                # normalized by WebSearchResult's validator.
                if result.link in seen_links:
                    continue
                seen_links.add(result.link)
                results.append(result)

                if len(results) >= self._num_results:
                    return results

        if not results:
            logger.info("SearXNG returned no usable results for query: %s", query)
        return results

    # -------------------------------------------------------------- connection

    def test_connection(self) -> dict[str, str]:
        try:
            response = requests.get(
                f"{self._base_url}/config", timeout=self._timeout_seconds
            )
            response.raise_for_status()
        except requests.Timeout as e:
            raise _config_error(
                f"SearXNG did not respond within {self._timeout_seconds}s. "
                "Check that the URL is reachable from the Onyx server."
            ) from e
        except requests.ConnectionError as e:
            raise _config_error(
                f"Could not connect to {self._base_url}. Check the URL and that "
                "the Onyx server can reach it on the network."
            ) from e
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code == 429:
                raise _config_error(
                    "This SearXNG instance rate-limited the request. Use a "
                    "private instance with the limiter disabled, or configure "
                    "it to allow this client."
                ) from e
            if status_code == 404:
                raise _config_error(
                    "No SearXNG instance was found at this URL. Please check it "
                    "and try again."
                ) from e
            raise _config_error(
                f"SearXNG connection failed (status {status_code})."
            ) from e

        # `/config` on a real SearXNG advertises the project's own git URL. Any
        # other site would have to be impersonating SearXNG for this to pass.
        try:
            config = response.json()
        except ValueError as e:
            raise _config_error(
                "This does not appear to be a SearXNG instance (its /config "
                "endpoint did not return JSON)."
            ) from e

        if (
            config.get("brand", {}).get("GIT_URL")
            != "https://github.com/searxng/searxng"
        ):
            raise _config_error(
                "This does not appear to be a SearXNG instance. Please check "
                "the URL and try again."
            )

        # A reachable instance with JSON output disabled passes every check
        # above and then fails on every real search, so prove the API works.
        self._assert_json_format_enabled()

        logger.info("Web search provider test succeeded for SearXNG.")
        return {"status": "ok"}

    def _assert_json_format_enabled(self) -> None:
        """Run one real search to prove `search.formats` includes `json`."""
        try:
            response = requests.post(
                f"{self._base_url}/search",
                data={"q": "onyx", "format": "json"},
                headers={"Accept": "application/json"},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            response.json()
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code == 403:
                raise _config_error(
                    "SearXNG refused the search request with 403, which means "
                    "JSON output is not enabled on this instance. Add 'json' to "
                    "the 'search.formats' list in its settings.yml and restart it."
                ) from e
            raise _config_error(
                f"SearXNG rejected a test search (status {status_code})."
            ) from e
        except ValueError as e:
            raise _config_error(
                "SearXNG did not return JSON for a test search. Add 'json' to "
                "the 'search.formats' list in its settings.yml and restart it."
            ) from e
        except requests.RequestException as e:
            raise _config_error(f"SearXNG test search failed: {e}") from e
