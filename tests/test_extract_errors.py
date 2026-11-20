"""What a failed extraction tells the person who uploaded the file.

A validator trace is the right thing to log and the wrong thing to show someone who
uploaded the wrong PDF. These run with no API key.
"""

from __future__ import annotations

from lab_explainer.extract import ExtractionFailed, _json_from, failure_message


def test_a_response_with_no_test_rows_reads_as_the_wrong_file():
    assert failure_message('{"report_id": null, "results": []}') == ExtractionFailed.NOT_A_REPORT


def test_whitespace_does_not_change_that():
    assert failure_message('{\n  "results" : [\n  ]\n}') == ExtractionFailed.NOT_A_REPORT


def test_a_malformed_report_reads_as_unreadable():
    assert failure_message('{"results": [{"test_id": "cbc.wbc"}]}') == ExtractionFailed.UNREADABLE


def test_the_two_messages_are_different_and_mention_no_internals():
    for msg in (ExtractionFailed.NOT_A_REPORT, ExtractionFailed.UNREADABLE):
        assert "pydantic" not in msg.lower()
        assert "validation" not in msg.lower()
    assert ExtractionFailed.NOT_A_REPORT != ExtractionFailed.UNREADABLE


def test_the_technical_detail_is_still_carried_for_logs():
    e = ExtractionFailed("2 validation errors for ExtractedReport", raw="{}")
    assert "validation errors" in e.detail
    assert e.message != e.detail


def test_json_is_recovered_from_a_fenced_response():
    assert _json_from('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_is_recovered_from_a_response_wrapped_in_prose():
    assert _json_from('Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}
