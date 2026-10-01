"""Tests for upload admission (content type and size).

Validates: Requirements 5.4, 5.5
"""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st
from hypothesis import settings as hypothesis_settings

from app.config import get_settings
from app.uploads import UploadRejectedError, validate_upload

settings = get_settings()


# --- unit tests ---------------------------------------------------------


def test_pdf_within_limit_is_accepted():
    validate_upload("application/pdf", 1024)


def test_non_pdf_is_rejected():
    with pytest.raises(UploadRejectedError):
        validate_upload("image/png", 1024)


def test_oversized_pdf_is_rejected():
    with pytest.raises(UploadRejectedError):
        validate_upload("application/pdf", settings.max_upload_bytes + 1)


def test_zero_byte_upload_is_rejected():
    with pytest.raises(UploadRejectedError):
        validate_upload("application/pdf", 0)


def test_exactly_at_limit_is_accepted():
    validate_upload("application/pdf", settings.max_upload_bytes)


# --- property test -------------------------------------------------------
# P6 — Upload admission respects type and size. For any declared content
# type and byte size, the upload is admitted if and only if the type is PDF
# and the size is within the configured limit. Validates: Requirements 5.4, 5.5

content_type_st = st.one_of(
    st.just("application/pdf"),
    st.sampled_from(["image/png", "text/csv", "application/zip", "application/octet-stream", ""]),
    st.text(min_size=0, max_size=20),
)
byte_size_st = st.integers(min_value=-10, max_value=settings.max_upload_bytes * 2)


@hypothesis_settings(max_examples=25)
@given(content_type=content_type_st, byte_size=byte_size_st)
def test_p6_admission_matches_type_and_size(content_type, byte_size):
    should_admit = (
        content_type in settings.allowed_content_types
        and 0 < byte_size <= settings.max_upload_bytes
    )
    if should_admit:
        validate_upload(content_type, byte_size)
    else:
        with pytest.raises(UploadRejectedError):
            validate_upload(content_type, byte_size)
