import io
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from urllib.parse import urlencode

import requests
from pydantic import ValidationError
from typing_extensions import override

from onyx.configs.constants import DocumentSource
from onyx.context.search.models import ChunkIndexRequest
from onyx.context.search.models import IndexFilters
from onyx.context.search.models import InferenceChunk
from onyx.federated_connectors.interfaces import FederatedConnector
from onyx.federated_connectors.m365.models import M365Config
from onyx.federated_connectors.m365.models import M365Credentials
from onyx.federated_connectors.models import CredentialField
from onyx.federated_connectors.models import EntityField
from onyx.federated_connectors.models import OAuthResult
from onyx.file_processing.extract_file_text import extract_file_text
from onyx.file_processing.extract_file_text import get_file_ext
from onyx.file_processing.file_types import OnyxFileExtensions
from onyx.onyxbot.slack.models import SlackContext
from onyx.utils.logger import setup_logger
from onyx.utils.threadpool_concurrency import run_functions_tuples_in_parallel

logger = setup_logger()

SCOPES = [
    "openid",
    "email",
    "profile",
    "offline_access",
    "Files.Read.All",
    "Sites.Read.All",
    "Mail.Read",
]

MICROSOFT_AUTH_BASE = "https://login.microsoftonline.com"
MICROSOFT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# --- File content enrichment ------------------------------------------------
# Graph's search API only returns a one-line `summary` per hit, which is far too
# thin for the LLM to answer from. For the top-ranked hits we download the file
# itself and extract its real text. Each download is an extra Graph round-trip,
# so these bounds trade latency and memory for answer quality.

# How many of the top-ranked file hits get their content downloaded.
MAX_FILES_TO_ENRICH = int(os.environ.get("M365_MAX_FILES_TO_ENRICH", "10"))
# Files bigger than this are skipped without being downloaded at all.
MAX_FILE_DOWNLOAD_BYTES = int(
    os.environ.get("M365_MAX_FILE_DOWNLOAD_BYTES", str(10 * 1024 * 1024))
)
# Extracted text is truncated here so one large file can't crowd every other
# result out of the LLM context window.
MAX_FILE_TEXT_CHARS = int(os.environ.get("M365_MAX_FILE_TEXT_CHARS", "8000"))
FILE_DOWNLOAD_TIMEOUT_SECONDS = int(
    os.environ.get("M365_FILE_DOWNLOAD_TIMEOUT_SECONDS", "20")
)
# Downloads run concurrently, but stay bounded so one query can't saturate the
# worker's thread pool or its Graph rate limit.
FILE_DOWNLOAD_MAX_WORKERS = int(os.environ.get("M365_FILE_DOWNLOAD_MAX_WORKERS", "5"))
# Federated results are the files a user chose *not* to index precisely because
# they are personal, so by default we parse them in-process rather than handing
# the bytes to the Unstructured cloud API even when one is configured for the
# indexing pipeline. Set to "true" only if that egress is acceptable to you.
ALLOW_UNSTRUCTURED_API = (
    os.environ.get("M365_ALLOW_UNSTRUCTURED_API", "false").lower() == "true"
)


class M365FederatedConnector(FederatedConnector):
    def __init__(self, credentials: dict[str, Any]) -> None:
        self.m365_credentials = M365Credentials(**credentials)

    @override
    def validate_entities(self, entities: dict[str, Any]) -> bool:
        """Validate that the provided entities match the expected structure.

        For M365 federated search, we expect:
        - search_scope: str (one of 'all', 'onedrive_only', 'sharepoint_only')
        - file_types: optional str (comma-separated file extensions)
        - max_results: int
        """
        try:
            M365Config(**entities)
            return True
        except ValidationError as e:
            logger.warning(f"Validation error for M365 entities: {e}")
            return False
        except Exception as e:
            logger.error(f"Error validating M365 entities: {e}")
            return False

    @classmethod
    def entities_schema(cls) -> dict[str, EntityField]:
        """Return the specifications of what entity configuration fields are available for M365."""
        return {
            "search_scope": EntityField(
                type="enum",
                description=(
                    "Scope of the search. 'all' searches OneDrive and SharePoint, "
                    "'onedrive_only' searches only OneDrive, "
                    "'sharepoint_only' searches only SharePoint."
                ),
                required=False,
                default="all",
                example="all",
            ),
            "file_types": EntityField(
                type="str",
                description=(
                    "Comma-separated list of file extensions to filter results "
                    "(e.g. 'docx,pdf,xlsx'). Leave empty to include all file types."
                ),
                required=False,
                default=None,
                example="docx,pdf,xlsx",
            ),
            "max_results": EntityField(
                type="int",
                description="Maximum number of search results to return per query.",
                required=False,
                default=25,
                example=25,
            ),
        }

    @classmethod
    @override
    def configuration_schema(cls) -> dict[str, EntityField]:
        """Return the specification of what configuration fields are available for M365."""
        return cls.entities_schema()

    @classmethod
    @override
    def credentials_schema(cls) -> dict[str, CredentialField]:
        """Return the specification of what credentials are required for M365 connector."""
        return {
            "client_id": CredentialField(
                type="str",
                description="Azure AD application (client) ID",
                required=True,
                example="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                secret=False,
            ),
            "client_secret": CredentialField(
                type="str",
                description="Azure AD application client secret",
                required=True,
                example="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                secret=True,
            ),
            "tenant_id": CredentialField(
                type="str",
                description="Azure AD tenant (directory) ID",
                required=True,
                example="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                secret=False,
            ),
        }

    @override
    def authorize(self, redirect_uri: str) -> str:
        """Generate the Microsoft OAuth2 authorization URL.

        Returns the URL where users should be redirected to authorize the application.
        Note: State parameter will be added by the API layer.
        """
        tenant_id = self.m365_credentials.tenant_id.strip()

        params = {
            "client_id": self.m365_credentials.client_id.strip(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "response_mode": "query",
        }

        # Use tenant ID directly — strip any whitespace/newlines
        oauth_url = (
            f"{MICROSOFT_AUTH_BASE}/{tenant_id}/oauth2/v2.0/authorize?"
            f"{urlencode(params)}"
        )

        logger.info(f"OAuth URL tenant: [{tenant_id}]")

        logger.info("Generated Microsoft OAuth authorization URL")
        return oauth_url

    @override
    def callback(self, callback_data: dict[str, Any], redirect_uri: str) -> OAuthResult:
        """Handle the response from the OAuth flow and return it in a standard format.

        Args:
            callback_data: The data received from the OAuth callback
                (state already validated by API layer)
            redirect_uri: The OAuth redirect URI used in the authorization request

        Returns:
            Standardized OAuthResult
        """
        auth_code = callback_data.get("code")
        error = callback_data.get("error")
        error_description = callback_data.get("error_description")

        if error:
            raise RuntimeError(f"OAuth error received: {error} - {error_description}")

        if not auth_code:
            raise ValueError("No authorization code received")

        token_response = self._exchange_code_for_token(auth_code, redirect_uri)

        access_token = token_response.get("access_token")
        refresh_token = token_response.get("refresh_token")
        token_type = token_response.get("token_type", "bearer")
        scope = token_response.get("scope")

        expires_at = None
        if "expires_in" in token_response:
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=token_response["expires_in"]
            )

        # Fetch user info from Microsoft Graph
        user_info = None
        if access_token:
            try:
                user_info = self._get_user_info(access_token)
            except Exception as e:
                logger.warning(f"Could not fetch user info from Microsoft Graph: {e}")

        return OAuthResult(
            access_token=access_token,
            token_type=token_type,
            scope=scope,
            expires_at=expires_at,
            refresh_token=refresh_token,
            user=user_info,
            raw_response=token_response,
        )

    def _exchange_code_for_token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchange authorization code for access token.

        Args:
            code: Authorization code from OAuth callback
            redirect_uri: The redirect URI used in the authorization request

        Returns:
            Token response from Microsoft identity platform
        """
        tenant_id = self.m365_credentials.tenant_id
        token_url = f"{MICROSOFT_AUTH_BASE}/{tenant_id}/oauth2/v2.0/token"

        response = requests.post(
            token_url,
            data={
                "client_id": self.m365_credentials.client_id,
                "client_secret": self.m365_credentials.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": " ".join(SCOPES),
            },
        )
        response.raise_for_status()
        return response.json()

    def _refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Refresh an expired access token using the refresh token.

        Args:
            refresh_token: The refresh token from a previous token exchange

        Returns:
            New token response from Microsoft identity platform
        """
        tenant_id = self.m365_credentials.tenant_id
        token_url = f"{MICROSOFT_AUTH_BASE}/{tenant_id}/oauth2/v2.0/token"

        response = requests.post(
            token_url,
            data={
                "client_id": self.m365_credentials.client_id,
                "client_secret": self.m365_credentials.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(SCOPES),
            },
        )
        response.raise_for_status()
        return response.json()

    def _get_user_info(self, access_token: str) -> dict[str, Any]:
        """Fetch user profile information from Microsoft Graph.

        Args:
            access_token: A valid access token

        Returns:
            Dictionary with user info (id, displayName, mail)
        """
        response = requests.get(
            f"{MICROSOFT_GRAPH_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return {
            "id": data.get("id"),
            "name": data.get("displayName"),
            "email": data.get("mail") or data.get("userPrincipalName"),
        }

    @override
    def search(
        self,
        query: ChunkIndexRequest,
        entities: dict[str, Any],
        access_token: str,
        limit: int | None = None,
        slack_event_context: SlackContext | None = None,
        bot_token: str | None = None,
    ) -> list[InferenceChunk]:
        """Perform a federated search on Microsoft 365 via Microsoft Graph API.

        Args:
            query: The search query
            entities: Connector-level config (entity filtering configuration)
            access_token: The OAuth access token
            limit: Maximum number of results to return
            slack_event_context: Not used for M365
            bot_token: Not used for M365

        Returns:
            Search results as a list of InferenceChunk
        """
        logger.debug(f"M365 federated search called with entities: {entities}")

        # Parse configuration
        try:
            config = M365Config(**entities)
        except ValidationError as e:
            logger.error(f"Invalid M365 configuration: {e}")
            return []

        max_results = limit if limit is not None else config.max_results

        # Build the query string, optionally filtering by file type
        query_string = query.query
        if config.file_types:
            extensions = [
                ext.strip() for ext in config.file_types.split(",") if ext.strip()
            ]
            if extensions:
                file_type_filter = " OR ".join(f"filetype:{ext}" for ext in extensions)
                query_string = f"({query_string}) AND ({file_type_filter})"

        # Microsoft Graph v1.0 /search/query does NOT support multiple entity
        # types in a single request.  We must issue separate calls for files
        # (driveItem) and emails (message), then merge the results.

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        all_chunks: list[InferenceChunk] = []

        # --- 1) File search (driveItem) ---
        file_query_string = query_string
        if config.search_scope == "sharepoint_only":
            file_query_string = f'({query_string}) AND (path:"https://*/sites/*")'
        elif config.search_scope == "onedrive_only":
            file_query_string = f'({query_string}) AND (path:"https://*/personal/*")'

        file_request: dict[str, Any] = {
            "requests": [
                {
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": file_query_string},
                    "from": 0,
                    "size": max_results,
                }
            ]
        }

        try:
            file_response = requests.post(
                f"{MICROSOFT_GRAPH_BASE}/search/query",
                headers=headers,
                json=file_request,
            )
            file_response.raise_for_status()
            all_chunks.extend(
                self._parse_file_search_response(file_response.json(), access_token)
            )
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"Microsoft Graph file search HTTP error: {e.response.status_code} "
                f"- {e.response.text}"
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Microsoft Graph file search request error: {e}")

        # --- 2) Email search (message) ---
        email_request: dict[str, Any] = {
            "requests": [
                {
                    "entityTypes": ["message"],
                    "query": {"queryString": query_string},
                    "from": 0,
                    "size": max_results,
                }
            ]
        }

        try:
            email_response = requests.post(
                f"{MICROSOFT_GRAPH_BASE}/search/query",
                headers=headers,
                json=email_request,
            )
            email_response.raise_for_status()
            all_chunks.extend(self._parse_search_response(email_response.json()))
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"Microsoft Graph email search HTTP error: {e.response.status_code} "
                f"- {e.response.text}"
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Microsoft Graph email search request error: {e}")

        return all_chunks

    @staticmethod
    def _collect_hits(response_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten the nested Graph search response down to a list of hits."""
        hits: list[dict[str, Any]] = []

        search_responses: list[dict[str, Any]] = response_data.get("value", [])
        for search_response in search_responses:
            hits_containers: list[dict[str, Any]] = search_response.get(
                "hitsContainers", []
            )
            for container in hits_containers:
                hits.extend(container.get("hits", []))

        return hits

    def _fetch_file_text(
        self, resource: dict[str, Any], access_token: str
    ) -> str | None:
        """Download one driveItem and extract its text, or None if unavailable.

        PRIVACY: the download is authorized with ``access_token`` -- the OAuth
        token of the user who asked the question -- and never with the
        application credentials held in ``self.m365_credentials``. Microsoft
        Graph therefore evaluates the request against that one user's own
        permissions: a file they cannot open in OneDrive/SharePoint returns
        403/404 here too, so one user can never pull another user's content.

        The extracted text is returned to the caller and lives only for the
        duration of this request. It is never written to Vespa, Postgres,
        Redis, or disk, and is never cached across users or queries.
        """
        name: str = resource.get("name", "")
        if not name:
            return None

        # Only bother with formats extract_file_text can actually read; images
        # and unknown binaries would just burn a round-trip.
        extension = get_file_ext(name)
        if extension not in OnyxFileExtensions.TEXT_AND_DOCUMENT_EXTENSIONS:
            return None

        # Skip oversized files before downloading rather than after.
        size: int | None = resource.get("size")
        if size is not None and size > MAX_FILE_DOWNLOAD_BYTES:
            logger.debug(
                "Skipping M365 file content fetch, file exceeds size cap: %s (%s bytes)",
                name,
                size,
            )
            return None

        parent_ref: dict[str, Any] = resource.get("parentReference", {})
        drive_id: str = parent_ref.get("driveId", "")
        item_id: str = resource.get("id", "")
        if not drive_id or not item_id:
            return None

        url = f"{MICROSOFT_GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"

        try:
            # Graph 302-redirects to a short-lived pre-authenticated download
            # URL. `requests` drops the Authorization header when the redirect
            # crosses hosts, so the user's token is never sent to the CDN.
            with requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                stream=True,
                timeout=FILE_DOWNLOAD_TIMEOUT_SECONDS,
            ) as response:
                response.raise_for_status()

                # Stream with a hard ceiling so a file that lied about (or
                # omitted) its size can't exhaust worker memory.
                buffer = io.BytesIO()
                downloaded = 0
                for block in response.iter_content(chunk_size=64 * 1024):
                    downloaded += len(block)
                    if downloaded > MAX_FILE_DOWNLOAD_BYTES:
                        logger.debug(
                            "Aborting M365 file content fetch, stream exceeded "
                            "size cap: %s",
                            name,
                        )
                        return None
                    buffer.write(block)
        except requests.exceptions.HTTPError as e:
            # 403/404 here is the expected outcome when the user lacks access to
            # a file that Graph search still surfaced metadata for -- not a bug.
            logger.debug(
                "M365 file content fetch failed for %s: HTTP %s",
                name,
                e.response.status_code,
            )
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("M365 file content fetch request error for %s: %s", name, e)
            return None

        try:
            text = extract_file_text(
                buffer,
                name,
                break_on_unprocessable=False,
                extension=extension,
                allow_unstructured=ALLOW_UNSTRUCTURED_API,
            )
        except Exception as e:
            logger.warning("Failed to extract text from M365 file %s: %s", name, e)
            return None

        if not text.strip():
            return None

        return text[:MAX_FILE_TEXT_CHARS]

    def _parse_file_search_response(
        self, response_data: dict[str, Any], access_token: str
    ) -> list[InferenceChunk]:
        """Parse driveItem hits, giving the top-ranked ones their real content.

        Hits beyond ``MAX_FILES_TO_ENRICH`` still come back, just with Graph's
        summary as their content the way they always did.
        """
        hits = self._collect_hits(response_data)
        if not hits:
            return []

        # Graph ranks the best hit as 1, so ascending order is most-relevant
        # first. Hits without a rank sort last.
        ranked = sorted(hits, key=lambda hit: hit.get("rank") or len(hits) + 1)
        to_enrich = ranked[:MAX_FILES_TO_ENRICH]

        fetched_texts: list[str | None] = []
        if to_enrich:
            fetched_texts = run_functions_tuples_in_parallel(
                [
                    (self._fetch_file_text, (hit.get("resource", {}), access_token))
                    for hit in to_enrich
                ],
                allow_failures=True,
                max_workers=FILE_DOWNLOAD_MAX_WORKERS,
            )

        enriched_count = sum(1 for text in fetched_texts if text)
        logger.info(
            "M365 file search: %s hits, %s of %s attempted downloads yielded text",
            len(ranked),
            enriched_count,
            len(to_enrich),
        )

        chunks: list[InferenceChunk] = []
        for index, hit in enumerate(ranked):
            resource: dict[str, Any] = hit.get("resource", {})
            if not resource:
                continue

            full_text = fetched_texts[index] if index < len(fetched_texts) else None
            try:
                chunk = self._file_to_inference_chunk(
                    hit, resource, full_text=full_text
                )
                if chunk is not None:
                    chunks.append(chunk)
            except Exception as e:
                logger.warning("Failed to parse M365 file search hit: %s", e)
                continue

        return chunks

    def _parse_search_response(
        self, response_data: dict[str, Any]
    ) -> list[InferenceChunk]:
        """Parse the Microsoft Graph search response into InferenceChunk objects.

        Args:
            response_data: Raw JSON response from Microsoft Graph search API

        Returns:
            List of InferenceChunk objects
        """
        chunks: list[InferenceChunk] = []

        for hit in self._collect_hits(response_data):
            try:
                chunk = self._hit_to_inference_chunk(hit)
                if chunk is not None:
                    chunks.append(chunk)
            except Exception as e:
                logger.warning(f"Failed to parse M365 search hit: {e}")
                continue

        return chunks

    def _hit_to_inference_chunk(self, hit: dict[str, Any]) -> InferenceChunk | None:
        """Convert a single Microsoft Graph search hit to an InferenceChunk.

        Handles both driveItem (files) and message (emails) result types.

        Args:
            hit: A single hit from the Microsoft Graph search response

        Returns:
            InferenceChunk or None if the hit cannot be parsed
        """
        resource: dict[str, Any] = hit.get("resource", {})
        if not resource:
            return None

        # Detect if this is an email or a file
        odata_type: str = resource.get("@odata.type", "")
        is_email = "message" in odata_type.lower()

        if is_email:
            return self._email_to_inference_chunk(hit, resource)
        else:
            return self._file_to_inference_chunk(hit, resource)

    def _email_to_inference_chunk(
        self, hit: dict[str, Any], resource: dict[str, Any]
    ) -> InferenceChunk | None:
        """Convert an email search hit to an InferenceChunk."""
        resource_id: str = resource.get("id", "")
        subject: str = resource.get("subject", "No Subject")
        web_link: str = resource.get("webLink", "")
        preview: str = resource.get("bodyPreview", "")
        received: str | None = resource.get("receivedDateTime")

        # Sender info
        sender_data: dict[str, Any] = resource.get("sender", {}).get("emailAddress", {})
        sender_name: str = sender_data.get("name", "")
        sender_email: str = sender_data.get("address", "")
        sender: str = f"{sender_name} <{sender_email}>" if sender_email else sender_name

        # Build content
        summary: str = hit.get("summary", preview)
        content = f"From: {sender}\nSubject: {subject}\n\n{summary}"

        # Metadata
        metadata: dict[str, str | list[str]] = {
            "type": "email",
            "sender": sender,
            "subject": subject,
        }
        if received:
            metadata["received"] = received

        # Parse date
        updated_at: datetime | None = None
        if received:
            try:
                updated_at = datetime.fromisoformat(received.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        rank: int = hit.get("rank", 0)

        return InferenceChunk(
            document_id=f"email_{resource_id}",
            chunk_id=0,
            blurb=f"Email: {subject}",
            content=content,
            source_links={0: web_link} if web_link else None,
            image_file_id=None,
            section_continuation=False,
            source_type=DocumentSource.NOT_APPLICABLE,
            semantic_identifier=f"Email: {subject} (from {sender})",
            title=subject,
            boost=0,
            score=float(rank) if rank else 0.0,
            hidden=False,
            metadata=metadata,
            match_highlights=[summary] if summary else [],
            doc_summary=summary,
            chunk_context="",
            updated_at=updated_at,
            is_federated=True,
        )

    def _file_to_inference_chunk(
        self,
        hit: dict[str, Any],
        resource: dict[str, Any],
        full_text: str | None = None,
    ) -> InferenceChunk | None:
        """Convert a file/driveItem search hit to an InferenceChunk.

        ``full_text`` is the file's extracted text when we were able to
        download it; when it is None we fall back to Graph's summary snippet.
        """
        resource_id: str = resource.get("id", "")
        name: str = resource.get("name", "Unknown")
        web_url: str = resource.get("webUrl", "")
        size: int | None = resource.get("size")
        last_modified: str | None = resource.get("lastModifiedDateTime")

        # Extract path from parentReference
        parent_ref: dict[str, Any] = resource.get("parentReference", {})
        path: str = parent_ref.get("path", "")
        site_name: str = parent_ref.get("siteId", "")

        # Build content from summary or hit highlights
        summary: str = hit.get("summary", "")

        # Extract highlights if available
        highlights: list[str] = []
        hit_highlights: list[dict[str, Any]] = hit.get("resource", {}).get(
            "_summary", []
        )
        if isinstance(hit_highlights, str):
            highlights.append(hit_highlights)
        elif isinstance(hit_highlights, list):
            for hl in hit_highlights:
                if isinstance(hl, str):
                    highlights.append(hl)

        # Prefer the file's real extracted text -- the summary is a single line
        # and gives the LLM almost nothing to reason over.
        content = full_text or summary or name

        # Build metadata
        metadata: dict[str, str | list[str]] = {}
        if last_modified:
            metadata["last_modified"] = last_modified
        if size is not None:
            metadata["size"] = str(size)
        if path:
            metadata["path"] = path
        if site_name:
            metadata["site_id"] = site_name

        # Parse last_modified to datetime
        updated_at: datetime | None = None
        if last_modified:
            try:
                updated_at = datetime.fromisoformat(
                    last_modified.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        rank: int = hit.get("rank", 0)
        score = float(rank) if rank else 0.0

        return InferenceChunk(
            document_id=resource_id,
            chunk_id=0,
            blurb=name,
            content=content,
            source_links={0: web_url} if web_url else None,
            image_file_id=None,
            section_continuation=False,
            source_type=DocumentSource.SHAREPOINT,
            semantic_identifier=name,
            title=name,
            boost=0,
            score=score,
            hidden=False,
            metadata=metadata,
            match_highlights=highlights if highlights else [summary] if summary else [],
            doc_summary=summary,
            chunk_context="",
            updated_at=updated_at,
            is_federated=True,
        )


if __name__ == "__main__":
    # Smoke test against a real tenant, without needing Onyx running.
    #
    #   export M365_ACCESS_TOKEN=<a delegated user token>
    #   python -m onyx.federated_connectors.m365.federated_connector "quarterly report"
    #
    # The token must be a *delegated* one for a real signed-in user -- that is
    # the whole point of the check. The quickest source is Graph Explorer
    # (developer.microsoft.com/graph/graph-explorer): sign in as a test user,
    # consent to Files.Read.All and Sites.Read.All, and copy the access token.
    import sys

    search_query = sys.argv[1] if len(sys.argv) > 1 else "report"
    token = os.environ["M365_ACCESS_TOKEN"]

    # Credentials are unused by the search path (only the user token authorizes
    # it) but the constructor validates their shape.
    test_connector = M365FederatedConnector(
        {
            "client_id": os.environ.get("M365_CLIENT_ID", "unused-for-search"),
            "client_secret": os.environ.get("M365_CLIENT_SECRET", "unused-for-search"),
            "tenant_id": os.environ.get("M365_TENANT_ID", "unused-for-search"),
        }
    )

    # Filters are irrelevant here -- federated search never touches the index.
    results = test_connector.search(
        ChunkIndexRequest(
            query=search_query,
            filters=IndexFilters(access_control_list=None),
        ),
        entities={"search_scope": "all", "max_results": 10},
        access_token=token,
    )

    print(f"\n{len(results)} results for {search_query!r}\n")
    for result in results:
        kind = result.metadata.get("type", "file")
        # A chunk longer than the Graph summary is one we actually downloaded.
        enriched = result.doc_summary != result.content
        marker = "CONTENT" if enriched else "summary"
        print(f"[{marker:>7}] ({kind}) {result.semantic_identifier}")
        print(f"          {len(result.content)} chars: {result.content[:160]!r}\n")
