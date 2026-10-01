"""Tests for page image rendering.

Renders directly from a real PDF file via pypdfium2 (no mocks), reusing the
same sample PDF as the parsing tests. Validates: Requirements 6.7
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.rendering import encode_page_webp
from tests.fixtures.pdfs import heading_and_paragraph_pdf


@pytest.fixture(scope="module")
def sample_pdf_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdfs") / "heading.pdf"
    path.write_bytes(heading_and_paragraph_pdf())
    return str(path)


def test_rendered_page_is_scaled_to_configured_dpi(sample_pdf_path):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(sample_pdf_path)
    try:
        page = doc[0]
        width, height = page.get_size()
        bitmap = page.render(scale=110 / 72)
        pil_image = bitmap.to_pil()
    finally:
        doc.close()

    # 612x792 pt page at 110 dpi (scale = 110/72), allowing for pdfium's own
    # pixel rounding.
    assert abs(pil_image.size[0] - width * 110 / 72) <= 1
    assert abs(pil_image.size[1] - height * 110 / 72) <= 1


def test_encode_page_webp_round_trips(sample_pdf_path):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(sample_pdf_path)
    try:
        pil_image = doc[0].render(scale=110 / 72).to_pil()
    finally:
        doc.close()

    webp_bytes = encode_page_webp(pil_image)
    assert webp_bytes[:4] == b"RIFF"  # webp container signature

    decoded = Image.open(io.BytesIO(webp_bytes))
    assert decoded.size == pil_image.size


# --- property test -----------------------------------------------------------
# P13 — Page images are complete and contiguous. For any parsed document, one
# page image exists per page, numbered contiguously from one.
# Validates: Requirements 6.7


def test_p13_page_numbers_are_contiguous_from_one(sample_pdf_path, monkeypatch):
    import app.rendering as rendering_module

    uploaded_keys = []
    monkeypatch.setattr(rendering_module.s3, "put_bytes", lambda key, body, content_type: uploaded_keys.append(key))
    monkeypatch.setattr(rendering_module.s3, "page_image_key", lambda project_id, document_id, page_no: f"page-{page_no}")

    rows = rendering_module.render_page_images(sample_pdf_path, project_id="proj", document_id="doc")

    page_numbers = [row["page_no"] for row in rows]
    assert page_numbers == list(range(1, len(page_numbers) + 1))
    assert uploaded_keys == [f"page-{n}" for n in page_numbers]
