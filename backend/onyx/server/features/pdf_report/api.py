"""API endpoint for generating PDF reports and uploading them to S3.

This endpoint is designed to be exposed as an Onyx Custom Tool so the LLM
can call it when a user asks to generate a report, save a document, or
create a PDF.
"""

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.auth.users import current_user
from onyx.db.engine.sql_engine import get_session
from onyx.db.generated_report import create_generated_report
from onyx.db.models import User
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.utils.logger import setup_logger
from onyx.utils.pdf_generator import generate_presigned_url
from onyx.utils.pdf_generator import markdown_to_pdf
from onyx.utils.pdf_generator import upload_pdf_to_s3

logger = setup_logger()

router = APIRouter(prefix="/tools")


# ── Request / Response schemas ────────────────────────────────────────────────


class GeneratePdfRequest(BaseModel):
    title: str
    content: str


class GeneratePdfResponse(BaseModel):
    status: str
    report_id: str
    s3_object_key: str
    download_url: str


# ── OpenAPI schema for LLM tool discovery ─────────────────────────────────────

GENERATE_PDF_OPENAPI_SCHEMA = {
    "openapi": "3.1.0",
    "info": {
        "title": "PDF Report Generator",
        "version": "1.0.0",
        "description": (
            "Call this tool when the user asks to generate a report, save a "
            "document, or create a PDF. Pass the fully formatted markdown "
            "content of your answer into the 'content' parameter and provide "
            "a concise 'title'."
        ),
    },
    "paths": {
        "/api/tools/generate-pdf": {
            "post": {
                "operationId": "generate_pdf_report",
                "summary": "Generate a PDF report from markdown content and save it to S3",
                "description": (
                    "Call this tool when the user asks to generate a report, "
                    "save a document, or create a PDF. Pass the fully formatted "
                    "markdown content of your answer into the 'content' parameter "
                    "and provide a concise 'title'."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["title", "content"],
                                "properties": {
                                    "title": {
                                        "type": "string",
                                        "description": "A concise title for the PDF report.",
                                    },
                                    "content": {
                                        "type": "string",
                                        "description": (
                                            "The full markdown-formatted body "
                                            "content of the report."
                                        ),
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "PDF generated and uploaded successfully.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string"},
                                        "report_id": {"type": "string"},
                                        "s3_object_key": {"type": "string"},
                                        "download_url": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/generate-pdf")
def generate_pdf_report(
    request: GeneratePdfRequest,
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> GeneratePdfResponse:
    """Generate a styled PDF from markdown, upload it to S3, and record it in
    the database. Returns a pre-signed download URL valid for 1 hour."""

    if not request.title.strip():
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, "Title must not be empty.")
    if not request.content.strip():
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, "Content must not be empty.")

    # 1. Generate the PDF
    try:
        pdf_bytes = markdown_to_pdf(request.title, request.content)
    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        raise OnyxError(
            OnyxErrorCode.INTERNAL_ERROR,
            "Failed to generate PDF from the provided content.",
        )

    # 2. Upload to S3
    try:
        s3_object_key = upload_pdf_to_s3(pdf_bytes, request.title, user.id)
    except (RuntimeError, ValueError) as e:
        logger.error(f"S3 upload failed: {e}")
        raise OnyxError(
            OnyxErrorCode.BAD_GATEWAY,
            f"Failed to upload PDF to S3: {e}",
        )

    # 3. Record in the database (rollback-safe — only committed after S3 succeeds)
    try:
        report = create_generated_report(
            db_session=db_session,
            user_id=user.id,
            title=request.title,
            s3_object_key=s3_object_key,
        )
    except Exception as e:
        logger.error(f"Failed to save report metadata: {e}")
        raise OnyxError(
            OnyxErrorCode.INTERNAL_ERROR,
            "PDF was uploaded but failed to save report metadata.",
        )

    # 4. Generate a pre-signed download URL (1 hour)
    try:
        download_url = generate_presigned_url(s3_object_key, expires_in=3600)
    except RuntimeError as e:
        logger.error(f"Presigned URL generation failed: {e}")
        raise OnyxError(
            OnyxErrorCode.BAD_GATEWAY,
            "PDF was uploaded but failed to generate download URL.",
        )

    return GeneratePdfResponse(
        status="success",
        report_id=str(report.id),
        s3_object_key=s3_object_key,
        download_url=download_url,
    )


@router.get("/generate-pdf/schema")
def get_pdf_tool_schema(
    _: User = Depends(current_user),
) -> dict:
    """Return the OpenAPI schema for the PDF report tool so it can be
    registered as an Onyx Custom Tool."""
    return GENERATE_PDF_OPENAPI_SCHEMA
