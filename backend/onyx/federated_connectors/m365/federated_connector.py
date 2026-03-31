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
from onyx.context.search.models import InferenceChunk
from onyx.federated_connectors.interfaces import FederatedConnector
from onyx.federated_connectors.m365.models import M365Config
from onyx.federated_connectors.m365.models import M365Credentials
from onyx.federated_connectors.models import CredentialField
from onyx.federated_connectors.models import EntityField
from onyx.federated_connectors.models import OAuthResult
from onyx.onyxbot.slack.models import SlackContext
from onyx.utils.logger import setup_logger

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
            all_chunks.extend(self._parse_search_response(file_response.json()))
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

        search_responses: list[dict[str, Any]] = response_data.get("value", [])

        for search_response in search_responses:
            hits_containers: list[dict[str, Any]] = search_response.get(
                "hitsContainers", []
            )

            for container in hits_containers:
                hits: list[dict[str, Any]] = container.get("hits", [])

                for hit in hits:
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
        self, hit: dict[str, Any], resource: dict[str, Any]
    ) -> InferenceChunk | None:
        """Convert a file/driveItem search hit to an InferenceChunk."""
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

        content = summary if summary else name

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
