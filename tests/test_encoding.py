"""Tests for engine `&X` colour code translation."""

from __future__ import annotations

from app.encoding import colour_to_html


def test_plain_text_is_escaped() -> None:
    assert colour_to_html("hello <script>") == "hello &lt;script&gt;"


def test_known_colour_opens_span() -> None:
    out = colour_to_html("&Yhello")
    assert '<span class="cY">' in out
    assert "hello" in out
    # Open span is closed at end-of-string.
    assert out.endswith("</span>")


def test_reset_closes_all_spans() -> None:
    out = colour_to_html("&Yhello&n world")
    assert out.count("<span") == 1
    assert "</span>" in out
    assert " world" in out


def test_unknown_code_is_dropped() -> None:
    out = colour_to_html("&Zhello")
    assert "&Z" not in out
    assert out == "hello"


def test_q_and_Q_are_ignored() -> None:
    out = colour_to_html("&qbold&Qoff")
    assert out == "boldoff"


def test_nested_colours_close_in_order() -> None:
    out = colour_to_html("&YA&RB&n")
    # Two spans opened, two closed by &n.
    assert out.count("<span") == 2
    assert out.count("</span>") == 2
