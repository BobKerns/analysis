import json
import logging

from analysis.log import JsonLineFormatter


def test_json_line_formatter_includes_extra_fields() -> None:
    formatter = JsonLineFormatter()
    record = logging.LogRecord(
        name="analysis.test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test event",
        args=(),
        exc_info=None,
    )
    record.event = "test.event"
    record.item = "coffee pods"

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "test.event"
    assert payload["item"] == "coffee pods"
    assert payload["level"] == "INFO"
    assert payload["message"] == "test event"
    assert "ts" in payload


def test_json_line_formatter_standard_fields_not_duplicated() -> None:
    formatter = JsonLineFormatter()
    record = logging.LogRecord(
        name="analysis.test",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="bare message",
        args=(),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "WARNING"
    assert payload["message"] == "bare message"
    # built-in record attrs should not leak as extra top-level keys
    assert "lineno" not in payload
    assert "levelno" not in payload
    assert "pathname" not in payload
