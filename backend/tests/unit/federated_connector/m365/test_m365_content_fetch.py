"""Tests for M365 federated file-content enrichment.

The behaviour under test is that top-ranked file hits get their real text
downloaded and handed to the LLM. The privacy properties are what most of
these assert: the download must be authorized with the *asking user's* OAuth
token and nothing else, and nothing may be retained between requests.
"""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import requests

from onyx.federated_connectors.m365 import federated_connector as m365_module
from onyx.federated_connectors.m365.federated_connector import M365FederatedConnector

APP_CLIENT_SECRET = "app-level-secret-that-must-never-be-used-for-downloads"
ALICE_TOKEN = "alice-user-oauth-token"
BOB_TOKEN = "bob-user-oauth-token"


def _resource(
    name: str = "quarterly-report.txt",
    size: int | None = 1024,
    drive_id: str = "drive-1",
    item_id: str = "item-1",
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "webUrl": f"https://contoso.sharepoint.com/{name}",
        "parentReference": {"driveId": drive_id, "path": "/drive/root:"},
    }
    if size is not None:
        resource["size"] = size
    return resource


def _search_response(hits: list[dict[str, Any]]) -> dict[str, Any]:
    return {"value": [{"hitsContainers": [{"hits": hits}]}]}


def _mock_download(body: bytes, status: int = 200) -> MagicMock:
    """Build a mock of the context-managed streaming `requests.get` response."""
    response = MagicMock()
    response.status_code = status
    response.iter_content.return_value = [body]
    if status >= 400:
        error = requests.exceptions.HTTPError(response=MagicMock(status_code=status))
        response.raise_for_status.side_effect = error
    else:
        response.raise_for_status.return_value = None
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


@pytest.fixture
def connector() -> M365FederatedConnector:
    return M365FederatedConnector(
        {
            "client_id": "test-client-id",
            "client_secret": APP_CLIENT_SECRET,
            "tenant_id": "test-tenant-id",
        }
    )


class TestPrivacy:
    def test_download_is_authorized_with_the_asking_users_token_only(
        self, connector: M365FederatedConnector
    ) -> None:
        """The user's own token authorizes the download -- never the app secret.

        This is the property that keeps one user from reading another's files:
        Graph evaluates the request against the delegated token's permissions.
        """
        with patch.object(
            m365_module.requests, "get", return_value=_mock_download(b"report body")
        ) as mock_get:
            text = connector._fetch_file_text(_resource(), ALICE_TOKEN)

        assert text == "report body"

        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == f"Bearer {ALICE_TOKEN}"

        # The application credentials must appear nowhere in the request.
        assert APP_CLIENT_SECRET not in repr(mock_get.call_args)

    def test_file_the_user_cannot_open_yields_no_content(
        self, connector: M365FederatedConnector
    ) -> None:
        """A 403 from Graph means the user lacks access -- return nothing."""
        with patch.object(
            m365_module.requests, "get", return_value=_mock_download(b"", status=403)
        ):
            assert connector._fetch_file_text(_resource(), ALICE_TOKEN) is None

    def test_file_content_stays_out_of_the_persisted_fields(
        self, connector: M365FederatedConnector
    ) -> None:
        """Downloaded text must reach the LLM in memory but never Postgres.

        `SearchDoc` persists blurb/match_highlights/metadata for chat replay but
        deliberately not chunk content. Keeping the file body out of those
        fields is what stops private text from being written to the database.
        """
        secret = "CONFIDENTIAL salary figures for FY26"
        hits = [{"rank": 1, "summary": "a snippet", "resource": _resource()}]

        with patch.object(
            m365_module.requests, "get", return_value=_mock_download(secret.encode())
        ):
            chunks = connector._parse_file_search_response(
                _search_response(hits), ALICE_TOKEN
            )

        chunk = chunks[0]
        assert chunk.content == secret

        assert secret not in chunk.blurb
        assert not any(secret in highlight for highlight in chunk.match_highlights)
        assert secret not in str(chunk.metadata)
        assert chunk.doc_summary is not None and secret not in chunk.doc_summary

    def test_each_user_gets_only_their_own_content(
        self, connector: M365FederatedConnector
    ) -> None:
        """No cross-user caching: same file id, different tokens, no bleed."""
        alice_doc = _resource(name="alice-notes.txt", item_id="shared-id")
        bob_doc = _resource(name="bob-notes.txt", item_id="shared-id")

        with patch.object(m365_module.requests, "get") as mock_get:
            mock_get.return_value = _mock_download(b"alice private notes")
            alice_text = connector._fetch_file_text(alice_doc, ALICE_TOKEN)

            mock_get.return_value = _mock_download(b"bob private notes")
            bob_text = connector._fetch_file_text(bob_doc, BOB_TOKEN)

        assert alice_text == "alice private notes"
        assert bob_text == "bob private notes"

        # Two independent round-trips, each with its own bearer token -- the
        # second call must not be served from anything the first one left behind.
        assert mock_get.call_count == 2
        tokens = [c.kwargs["headers"]["Authorization"] for c in mock_get.call_args_list]
        assert tokens == [f"Bearer {ALICE_TOKEN}", f"Bearer {BOB_TOKEN}"]


class TestResourceBounds:
    def test_oversized_file_is_never_downloaded(
        self, connector: M365FederatedConnector
    ) -> None:
        oversized = _resource(size=m365_module.MAX_FILE_DOWNLOAD_BYTES + 1)

        with patch.object(m365_module.requests, "get") as mock_get:
            assert connector._fetch_file_text(oversized, ALICE_TOKEN) is None

        mock_get.assert_not_called()

    def test_stream_exceeding_the_cap_is_aborted(
        self, connector: M365FederatedConnector
    ) -> None:
        """A file that under-reports its size still can't exhaust memory."""
        body = b"x" * (m365_module.MAX_FILE_DOWNLOAD_BYTES + 1)

        with patch.object(
            m365_module.requests, "get", return_value=_mock_download(body)
        ):
            assert connector._fetch_file_text(_resource(size=10), ALICE_TOKEN) is None

    def test_unsupported_extension_is_never_downloaded(
        self, connector: M365FederatedConnector
    ) -> None:
        with patch.object(m365_module.requests, "get") as mock_get:
            assert (
                connector._fetch_file_text(_resource(name="logo.png"), ALICE_TOKEN)
                is None
            )

        mock_get.assert_not_called()

    def test_extracted_text_is_truncated(
        self, connector: M365FederatedConnector
    ) -> None:
        body = b"y" * (m365_module.MAX_FILE_TEXT_CHARS + 500)

        with patch.object(
            m365_module.requests, "get", return_value=_mock_download(body)
        ):
            text = connector._fetch_file_text(_resource(size=len(body)), ALICE_TOKEN)

        assert text is not None
        assert len(text) == m365_module.MAX_FILE_TEXT_CHARS

    def test_missing_drive_id_is_skipped(
        self, connector: M365FederatedConnector
    ) -> None:
        resource = _resource()
        resource["parentReference"] = {}

        with patch.object(m365_module.requests, "get") as mock_get:
            assert connector._fetch_file_text(resource, ALICE_TOKEN) is None

        mock_get.assert_not_called()


class TestChunkContent:
    def test_real_file_text_replaces_the_graph_summary(
        self, connector: M365FederatedConnector
    ) -> None:
        hits = [{"rank": 1, "summary": "a one line snippet", "resource": _resource()}]

        with patch.object(
            m365_module.requests,
            "get",
            return_value=_mock_download(b"the full body of the report"),
        ):
            chunks = connector._parse_file_search_response(
                _search_response(hits), ALICE_TOKEN
            )

        assert len(chunks) == 1
        assert chunks[0].content == "the full body of the report"
        # The summary is still preserved for display purposes.
        assert chunks[0].doc_summary == "a one line snippet"

    def test_falls_back_to_summary_when_download_fails(
        self, connector: M365FederatedConnector
    ) -> None:
        """An inaccessible file still shows up -- just without its content."""
        hits = [{"rank": 1, "summary": "a one line snippet", "resource": _resource()}]

        with patch.object(
            m365_module.requests, "get", return_value=_mock_download(b"", status=403)
        ):
            chunks = connector._parse_file_search_response(
                _search_response(hits), ALICE_TOKEN
            )

        assert len(chunks) == 1
        assert chunks[0].content == "a one line snippet"

    def test_only_the_top_ranked_hits_are_downloaded(
        self, connector: M365FederatedConnector
    ) -> None:
        over_limit = m365_module.MAX_FILES_TO_ENRICH + 3
        hits = [
            {
                "rank": rank,
                "summary": f"snippet {rank}",
                "resource": _resource(name=f"doc-{rank}.txt", item_id=f"item-{rank}"),
            }
            for rank in range(1, over_limit + 1)
        ]

        with patch.object(
            m365_module.requests, "get", return_value=_mock_download(b"downloaded body")
        ) as mock_get:
            chunks = connector._parse_file_search_response(
                _search_response(hits), ALICE_TOKEN
            )

        # Every hit is returned, but only the top N cost a download.
        assert len(chunks) == over_limit
        assert mock_get.call_count == m365_module.MAX_FILES_TO_ENRICH

        enriched = [c for c in chunks if c.content == "downloaded body"]
        assert len(enriched) == m365_module.MAX_FILES_TO_ENRICH
        # The un-enriched tail keeps the old summary behaviour.
        assert chunks[-1].content == f"snippet {over_limit}"

    def test_content_is_matched_to_the_right_file(
        self, connector: M365FederatedConnector
    ) -> None:
        """Parallel downloads must not cross-wire content onto another file.

        Downloads complete out of order, so if results were collected by
        completion rather than by index, one user's file could be displayed
        under a different file's name and link.
        """
        names = ["alpha.txt", "beta.txt", "gamma.txt", "delta.txt"]
        hits = [
            {
                "rank": rank,
                "summary": f"snippet for {name}",
                "resource": _resource(name=name, item_id=f"item-{rank}"),
            }
            for rank, name in enumerate(names, start=1)
        ]

        def fake_get(url: str, **kwargs: Any) -> MagicMock:  # noqa: ARG001
            item_id = url.split("/items/")[1].split("/")[0]
            rank = int(item_id.rsplit("-", 1)[1])
            return _mock_download(f"body of {names[rank - 1]}".encode())

        with patch.object(m365_module.requests, "get", side_effect=fake_get):
            chunks = connector._parse_file_search_response(
                _search_response(hits), ALICE_TOKEN
            )

        assert len(chunks) == len(names)
        for chunk, name in zip(chunks, names):
            assert chunk.semantic_identifier == name
            assert chunk.content == f"body of {name}"

    def test_empty_response_yields_no_chunks(
        self, connector: M365FederatedConnector
    ) -> None:
        assert connector._parse_file_search_response({}, ALICE_TOKEN) == []
