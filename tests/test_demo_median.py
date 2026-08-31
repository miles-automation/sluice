"""Deterministic checks for the non-deterministic demo harness."""

import json

from demo.median import _extract_result_text, prompt_for_rows


def test_prompt_and_grading_row_count_stay_aligned() -> None:
    prompt = prompt_for_rows(17)

    assert "requesting 17 rows" in prompt
    assert "across all 17 rows" in prompt


def test_extract_result_from_stream_json_transcript() -> None:
    transcript = "\n".join(
        [
            json.dumps({"type": "system", "model": "requested-alias"}),
            "not-json-but-preserved",
            json.dumps({"type": "assistant", "message": {"model": "resolved-model"}}),
            json.dumps({"type": "result", "result": "72.5"}),
        ]
    )

    assert _extract_result_text(transcript) == ("72.5", "resolved-model")
