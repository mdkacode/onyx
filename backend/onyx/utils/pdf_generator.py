"""Utility for generating PDFs from Markdown/HTML and uploading to S3."""

import io
import os
import re
from datetime import datetime
from datetime import timezone
from uuid import UUID

import boto3
import markdown
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

from onyx.utils.logger import setup_logger

logger = setup_logger()

# ── CSS styling for generated reports ─────────────────────────────────────────
REPORT_CSS = """
@page {
    size: A4;
    margin: 2cm 2.5cm;
    @top-center {
        content: "Generated Report";
        font-family: "Liberation Sans", "Helvetica Neue", Arial, sans-serif;
        font-size: 9px;
        color: #888888;
    }
    @bottom-center {
        content: "Page " counter(page) " of " counter(pages);
        font-family: "Liberation Sans", "Helvetica Neue", Arial, sans-serif;
        font-size: 9px;
        color: #888888;
    }
}

body {
    font-family: "Liberation Sans", "Helvetica Neue", Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.6;
    color: #222222;
}

h1 {
    font-size: 22pt;
    color: #1a1a1a;
    border-bottom: 2px solid #333333;
    padding-bottom: 6px;
    margin-top: 0;
}

h2 {
    font-size: 16pt;
    color: #2a2a2a;
    margin-top: 24px;
}

h3 {
    font-size: 13pt;
    color: #333333;
    margin-top: 18px;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
}

th, td {
    border: 1px solid #cccccc;
    padding: 8px 12px;
    text-align: left;
}

th {
    background-color: #f0f0f0;
    font-weight: bold;
}

code {
    background-color: #f4f4f4;
    padding: 2px 5px;
    border-radius: 3px;
    font-size: 10pt;
}

pre {
    background-color: #f4f4f4;
    padding: 12px;
    border-radius: 4px;
    overflow-x: auto;
    font-size: 10pt;
    line-height: 1.4;
}

blockquote {
    border-left: 4px solid #cccccc;
    margin: 12px 0;
    padding: 8px 16px;
    color: #555555;
}
"""


def _sanitize_title(title: str) -> str:
    """Convert a title into a safe filename component."""
    sanitized = re.sub(r"[^\w\s-]", "", title.lower().strip())
    sanitized = re.sub(r"[\s_]+", "-", sanitized)
    return sanitized[:80]


def _get_s3_client() -> "boto3.client":
    """Create an S3 client from environment variables."""
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def _get_s3_bucket() -> str:
    """Return the configured S3 bucket name."""
    bucket = os.environ.get("S3_REPORT_BUCKET")
    if not bucket:
        raise ValueError(
            "S3_REPORT_BUCKET environment variable is not set. "
            "Please configure it to enable PDF report generation."
        )
    return bucket


def markdown_to_pdf(title: str, content: str) -> bytes:
    """Convert a Markdown string to a styled PDF.

    Args:
        title: The report title (rendered as an H1 at the top).
        content: Markdown-formatted body content.

    Returns:
        The generated PDF as raw bytes.
    """
    html_body = markdown.markdown(
        content,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
    )

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <style>{REPORT_CSS}</style>
</head>
<body>
    <h1>{title}</h1>
    {html_body}
</body>
</html>"""

    from weasyprint import HTML  # lazy import — requires pango system libs

    pdf_buffer = io.BytesIO()
    HTML(string=full_html).write_pdf(pdf_buffer)
    pdf_buffer.seek(0)
    return pdf_buffer.read()


def upload_pdf_to_s3(
    pdf_bytes: bytes,
    title: str,
    user_id: UUID,
) -> str:
    """Upload a PDF to S3 and return the object key.

    Args:
        pdf_bytes: Raw PDF content.
        title: Report title (used in the object key).
        user_id: The owning user's ID (used in the object key).

    Returns:
        The S3 object key where the file was stored.

    Raises:
        RuntimeError: If the S3 upload fails.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_title = _sanitize_title(title)
    object_key = f"reports/{user_id}/{timestamp}_{safe_title}.pdf"

    bucket = _get_s3_bucket()
    s3_client = _get_s3_client()

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
    except (BotoCoreError, ClientError) as e:
        logger.error(f"S3 upload failed for key={object_key}: {e}")
        raise RuntimeError(f"Failed to upload PDF to S3: {e}") from e

    return object_key


def generate_presigned_url(object_key: str, expires_in: int = 3600) -> str:
    """Generate a pre-signed S3 URL for downloading a report.

    Args:
        object_key: The S3 object key.
        expires_in: URL expiry in seconds (default: 1 hour).

    Returns:
        A pre-signed URL string.
    """
    bucket = _get_s3_bucket()
    s3_client = _get_s3_client()

    try:
        url: str = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expires_in,
        )
    except (BotoCoreError, ClientError) as e:
        logger.error(f"Failed to generate presigned URL for key={object_key}: {e}")
        raise RuntimeError(f"Failed to generate presigned URL: {e}") from e

    return url
