"""Tests for the parse job handler's pure serialisation logic.

The full handler needs a live Postgres, S3 and the extraction service to
exercise end to end; this covers the part that doesn't: turning the
extraction service's result payload into the `parsed.json` bytes.
Validates: Requirements 6.8
"""

from __future__ import annotations

import json

from app.pipeline import _parsed_json


def test_parsed_json_round_trips_pages_and_blocks():
    result = {
        "pages": [{"page_no": 1, "width": 612.0, "height": 792.0}],
        "blocks": [
            {
                "page_no": 1,
                "text": "Net Asset Value",
                "bbox": [72.0, 74.7, 244.0, 97.0],
                "kind": "heading",
                "heading_path": "Net Asset Value",
            },
            {
                "page_no": 1,
                "text": "Reported NAV is $42.6m.",
                "bbox": [72.0, 133.3, 426.1, 144.4],
                "kind": "paragraph",
                "heading_path": "Net Asset Value",
            },
        ],
    }

    payload = json.loads(_parsed_json(result))

    assert payload["pages"] == [{"page_no": 1, "width": 612.0, "height": 792.0}]
    assert len(payload["blocks"]) == 2
    assert payload["blocks"][0]["text"] == "Net Asset Value"
    assert payload["blocks"][0]["bbox"] == [72.0, 74.7, 244.0, 97.0]
    assert payload["blocks"][1]["heading_path"] == "Net Asset Value"


def test_parsed_json_handles_no_blocks():
    result = {"pages": [{"page_no": 1, "width": 612.0, "height": 792.0}], "blocks": []}
    payload = json.loads(_parsed_json(result))
    assert payload["blocks"] == []
