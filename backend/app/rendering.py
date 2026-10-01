"""Page image rendering and upload.

Renders each page to webp at the configured dpi and uploads it, recording
the key in `document_pages`. Requirement 6.7.

Extraction (text, blocks) now happens out of process in the standalone
extraction service (task 8.2), so this renders directly from the local PDF
file via pypdfium2 rather than reusing a Docling conversion result. This
avoids a second full Docling conversion just to get page images.
"""

from __future__ import annotations

import io

import pypdfium2 as pdfium

from app import s3
from app.config import get_settings


def encode_page_webp(pil_image) -> bytes:
    """Encode one rendered page to webp bytes. Isolated so the encoding step
    is testable without S3.
    """
    buffer = io.BytesIO()
    pil_image.convert("RGB").save(buffer, format="WEBP", quality=85)
    return buffer.getvalue()


def render_page_images(pdf_path: str, *, project_id: str, document_id: str) -> list[dict]:
    """Render and upload one webp per page directly from the PDF file.

    Returns one row per page, ready to insert into `document_pages`:
    `{page_no, width, height, image_key}`. Requirement 6.7: one image per
    page, numbered contiguously from one — pages are 1-indexed to match the
    rest of the pipeline.
    """
    settings = get_settings()
    scale = settings.page_image_dpi / 72

    rows: list[dict] = []
    doc = pdfium.PdfDocument(pdf_path)
    try:
        for index, page in enumerate(doc):
            page_no = index + 1
            width, height = page.get_size()
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()

            webp_bytes = encode_page_webp(pil_image)
            key = s3.page_image_key(project_id, document_id, page_no)
            s3.put_bytes(key, webp_bytes, content_type="image/webp")

            rows.append({"page_no": page_no, "width": width, "height": height, "image_key": key})
    finally:
        doc.close()
    return rows


def persist_page_geometry(conn, document_id: str, rows: list[dict]) -> None:
    """Insert the page rows produced by `render_page_images`."""
    for row in rows:
        conn.execute(
            "insert into document_pages (document_id, page_no, width, height, image_key) "
            "values (%s, %s, %s, %s, %s) "
            "on conflict (document_id, page_no) do update set "
            "  width = excluded.width, height = excluded.height, image_key = excluded.image_key",
            (document_id, row["page_no"], row["width"], row["height"], row["image_key"]),
        )


__all__ = ["encode_page_webp", "render_page_images", "persist_page_geometry"]
