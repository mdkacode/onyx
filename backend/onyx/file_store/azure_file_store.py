from __future__ import annotations

import tempfile
import uuid
from io import BytesIO
from typing import Any
from typing import cast
from typing import IO
from typing import TYPE_CHECKING

import puremagic
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from azure.storage.blob import BlobServiceClient

from onyx.configs.constants import FileOrigin
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.engine.sql_engine import get_session_with_current_tenant_if_none
from onyx.db.file_record import delete_filerecord_by_file_id
from onyx.db.file_record import get_filerecord_by_file_id
from onyx.db.file_record import get_filerecord_by_file_id_optional
from onyx.db.file_record import get_filerecord_by_prefix
from onyx.db.file_record import upsert_filerecord
from onyx.db.models import FileRecord
from onyx.file_store.file_store import FileStore
from onyx.file_store.s3_key_utils import generate_s3_key
from onyx.utils.file import FileWithMimeType
from onyx.utils.logger import setup_logger
from shared_configs.contextvars import get_current_tenant_id

logger = setup_logger()


class AzureBlobFileStore(FileStore):
    """Azure Blob Storage backed file store.

    `FileRecord.bucket_name` holds the blob *container* name, which keeps the
    record shape identical to the S3/GCS stores so nothing downstream needs to
    know which backend wrote the row.
    """

    def __init__(
        self,
        container_name: str,
        account_name: str | None = None,
        account_key: str | None = None,
        connection_string: str | None = None,
        azure_prefix: str | None = None,
    ) -> None:
        self._blob_service_client: BlobServiceClient | None = None
        self._container_name = container_name
        self._account_name = account_name
        self._account_key = account_key
        self._connection_string = connection_string
        self._azure_prefix = azure_prefix or "onyx-files"

    def _get_blob_service_client(self) -> BlobServiceClient:
        """Initialize the Azure client if not already done.

        Authentication priority:
        1. Connection string (AZURE_STORAGE_CONNECTION_STRING)
        2. Account name + shared key (AZURE_STORAGE_ACCOUNT_NAME/_KEY)

        Managed Identity is deliberately not wired up here: it needs the
        `azure-identity` package, which is not in the image, so offering the
        branch would only fail at runtime. Add the dependency first if you
        want to drop the shared key.
        """
        if self._blob_service_client is None:
            try:
                from azure.storage.blob import BlobServiceClient

                if self._connection_string:
                    self._blob_service_client = (
                        BlobServiceClient.from_connection_string(
                            self._connection_string
                        )
                    )
                elif self._account_name and self._account_key:
                    self._blob_service_client = BlobServiceClient(
                        account_url=f"https://{self._account_name}.blob.core.windows.net",
                        credential=self._account_key,
                    )
                else:
                    raise RuntimeError(
                        "Azure file store requires AZURE_STORAGE_CONNECTION_STRING, "
                        "or AZURE_STORAGE_ACCOUNT_NAME together with "
                        "AZURE_STORAGE_ACCOUNT_KEY"
                    )

            except ImportError as e:
                logger.error(f"Failed to import azure-storage-blob: {e}")
                raise
            except Exception as e:
                logger.error(f"Failed to initialize Azure Blob client: {e}")
                raise RuntimeError(
                    f"Failed to initialize Azure Blob client: {e}"
                ) from e

        return self._blob_service_client

    def _get_object_key(self, file_name: str) -> str:
        """Generate a blob name from a file name, prefixed with the tenant ID.

        Reuses the S3 key utilities — S3-safe keys are a strict subset of
        Azure-safe blob names, and sharing the helper keeps object layout
        identical across backends so data can be copied between them verbatim.
        """
        tenant_id = get_current_tenant_id()
        key = generate_s3_key(
            file_name=file_name,
            prefix=self._azure_prefix,
            tenant_id=tenant_id,
            max_key_length=1024,
        )
        if len(key) == 1024:
            logger.info(f"File name was too long and was truncated: {file_name}")
        return key

    def initialize(self) -> None:
        """Ensure the configured container exists."""
        from azure.core.exceptions import ResourceExistsError

        client = self._get_blob_service_client()
        container_client = client.get_container_client(self._container_name)
        try:
            container_client.create_container()
            logger.info(f"Created Azure container '{self._container_name}'")
        except ResourceExistsError:
            logger.info(f"Azure container '{self._container_name}' already exists")

    def has_file(
        self,
        file_id: str,
        file_origin: FileOrigin,
        file_type: str,
        db_session: Session | None = None,
    ) -> bool:
        with get_session_with_current_tenant_if_none(db_session) as db_session:
            file_record = get_filerecord_by_file_id_optional(
                file_id=file_id, db_session=db_session
            )
        return (
            file_record is not None
            and file_record.file_origin == file_origin
            and file_record.file_type == file_type
        )

    def save_file(
        self,
        content: IO,
        display_name: str | None,
        file_origin: FileOrigin,
        file_type: str,
        file_metadata: dict[str, Any] | None = None,
        file_id: str | None = None,
        db_session: Session | None = None,
    ) -> str:
        from azure.storage.blob import ContentSettings

        if file_id is None:
            file_id = str(uuid.uuid4())

        client = self._get_blob_service_client()
        object_key = self._get_object_key(file_id)
        blob_client = client.get_blob_client(
            container=self._container_name, blob=object_key
        )

        if hasattr(content, "read"):
            file_content = content.read()
            if hasattr(content, "seek"):
                content.seek(0)
        else:
            file_content = content

        blob_client.upload_blob(
            file_content,
            overwrite=True,
            content_settings=ContentSettings(content_type=file_type),
        )

        try:
            with get_session_with_current_tenant_if_none(db_session) as db_session:
                upsert_filerecord(
                    file_id=file_id,
                    display_name=display_name or file_id,
                    file_origin=file_origin,
                    file_type=file_type,
                    bucket_name=self._container_name,
                    object_key=object_key,
                    db_session=db_session,
                    file_metadata=file_metadata,
                )
                db_session.commit()
        except Exception:
            try:
                blob_client.delete_blob()
            except Exception:
                logger.warning(
                    f"Failed to clean up orphaned Azure blob "
                    f"{self._container_name}/{object_key} after DB persistence "
                    f"failure for file {file_id}",
                    exc_info=True,
                )
            raise

        return file_id

    def read_file(
        self,
        file_id: str,
        mode: str | None = None,  # noqa: ARG002
        use_tempfile: bool = False,
        db_session: Session | None = None,
    ) -> IO[bytes]:
        with get_session_with_current_tenant_if_none(db_session) as db_session:
            file_record = get_filerecord_by_file_id(
                file_id=file_id, db_session=db_session
            )

        client = self._get_blob_service_client()
        blob_client = client.get_blob_client(
            container=file_record.bucket_name, blob=file_record.object_key
        )
        downloader = blob_client.download_blob()

        if use_tempfile:
            temp_file = tempfile.NamedTemporaryFile(mode="w+b", delete=True)
            downloader.readinto(temp_file)
            temp_file.seek(0)
            return temp_file

        # StorageStreamDownloader is generic over str|bytes; the blob is always
        # downloaded as bytes here since no encoding was requested.
        return BytesIO(cast(bytes, downloader.readall()))

    def read_file_record(
        self, file_id: str, db_session: Session | None = None
    ) -> FileRecord:
        with get_session_with_current_tenant_if_none(db_session) as db_session:
            file_record = get_filerecord_by_file_id(
                file_id=file_id, db_session=db_session
            )
        return file_record

    def get_file_size(
        self, file_id: str, db_session: Session | None = None
    ) -> int | None:
        """Get the size of a file in bytes from the blob properties."""
        try:
            with get_session_with_current_tenant_if_none(db_session) as db_session:
                file_record = get_filerecord_by_file_id(
                    file_id=file_id, db_session=db_session
                )

            client = self._get_blob_service_client()
            blob_client = client.get_blob_client(
                container=file_record.bucket_name, blob=file_record.object_key
            )
            return blob_client.get_blob_properties().size
        except Exception as e:
            logger.warning(f"Error getting file size for {file_id}: {e}")
            return None

    def delete_file(
        self,
        file_id: str,
        error_on_missing: bool = True,
        db_session: Session | None = None,
    ) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        with get_session_with_current_tenant_if_none(db_session) as db_session:
            try:
                file_record = get_filerecord_by_file_id_optional(
                    file_id=file_id, db_session=db_session
                )
                if file_record is None:
                    if error_on_missing:
                        raise RuntimeError(
                            f"File by id {file_id} does not exist or was deleted"
                        )
                    return
                if not file_record.bucket_name:
                    logger.error(
                        f"File record {file_id} with key {file_record.object_key} "  # noqa: S608 - log message, not SQL
                        "has no container name, cannot delete from filestore"
                    )
                    delete_filerecord_by_file_id(file_id=file_id, db_session=db_session)
                    db_session.commit()
                    return

                client = self._get_blob_service_client()
                blob_client = client.get_blob_client(
                    container=file_record.bucket_name, blob=file_record.object_key
                )
                try:
                    blob_client.delete_blob()
                except ResourceNotFoundError:
                    logger.warning(
                        f"delete_file: File {file_id} not found in Azure "
                        f"(key: {file_record.object_key}), "
                        "cleaning up database record."
                    )

                delete_filerecord_by_file_id(file_id=file_id, db_session=db_session)
                db_session.commit()

            except Exception:
                db_session.rollback()
                raise

    def change_file_id(
        self,
        old_file_id: str,
        new_file_id: str,
        db_session: Session | None = None,
    ) -> None:
        with get_session_with_current_tenant_if_none(db_session) as db_session:
            try:
                old_file_record = get_filerecord_by_file_id(
                    file_id=old_file_id, db_session=db_session
                )
                new_object_key = self._get_object_key(new_file_id)

                client = self._get_blob_service_client()
                source_blob = client.get_blob_client(
                    container=old_file_record.bucket_name,
                    blob=old_file_record.object_key,
                )
                dest_blob = client.get_blob_client(
                    container=self._container_name, blob=new_object_key
                )

                # Server-side copy would need the source exposed via SAS since
                # the container is private, so stream through a temp file
                # instead. Spooling to disk keeps large files off the heap.
                with tempfile.NamedTemporaryFile(mode="w+b") as buffer:
                    source_blob.download_blob().readinto(buffer)
                    buffer.seek(0)
                    dest_blob.upload_blob(buffer, overwrite=True)

                file_metadata = cast(
                    dict[Any, Any] | None, old_file_record.file_metadata
                )

                upsert_filerecord(
                    file_id=new_file_id,
                    display_name=old_file_record.display_name,
                    file_origin=old_file_record.file_origin,
                    file_type=old_file_record.file_type,
                    bucket_name=self._container_name,
                    object_key=new_object_key,
                    db_session=db_session,
                    file_metadata=file_metadata,
                )

                delete_filerecord_by_file_id(file_id=old_file_id, db_session=db_session)

                db_session.commit()

                try:
                    source_blob.delete_blob()
                except Exception:
                    logger.warning(
                        f"Failed to delete old Azure blob after changing file ID "
                        f"from {old_file_id} to {new_file_id}; blob may be orphaned",
                        exc_info=True,
                    )

            except Exception as e:
                db_session.rollback()
                logger.exception(
                    f"Failed to change file ID from {old_file_id} to {new_file_id}: {e}"
                )
                raise

    def get_file_with_mime_type(self, file_id: str) -> FileWithMimeType | None:
        mime_type: str = "application/octet-stream"
        try:
            file_io = self.read_file(file_id, mode="b")
            file_content = file_io.read()
            matches = puremagic.magic_string(file_content)
            if matches:
                mime_type = cast(str, matches[0].mime_type)
            return FileWithMimeType(data=file_content, mime_type=mime_type)
        except Exception:
            return None

    def list_files_by_prefix(self, prefix: str) -> list[FileRecord]:
        """List all file IDs that start with the given prefix."""
        with get_session_with_current_tenant() as db_session:
            file_records = get_filerecord_by_prefix(
                prefix=prefix, db_session=db_session
            )
        return file_records
