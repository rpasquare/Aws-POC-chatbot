"""Hand-built minimal PDFs for parser tests. No external PDF library needed:
each is a small, valid, single-content-stream PDF assembled directly from
the object/xref/trailer structure.
"""

from __future__ import annotations


def build_pdf(content: bytes, media_box: str = "0 0 612 792") -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [%s] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>" % media_box.encode()
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]

    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, obj)

    xref_offset = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1,
        xref_offset,
    )
    return pdf


def heading_and_paragraph_pdf() -> bytes:
    """One page: a large heading followed by a body paragraph beneath it."""
    content = (
        b"BT /F1 24 Tf 72 700 Td (Net Asset Value) Tj ET\n"
        b"BT /F1 12 Tf 72 650 Td "
        b"(Reported NAV for the interest as at 30 September 2025 is $42.6m.) Tj ET"
    )
    return build_pdf(content)


def blank_page_pdf() -> bytes:
    """One page with no text content at all (routes to OCR upstream)."""
    return build_pdf(b"")
