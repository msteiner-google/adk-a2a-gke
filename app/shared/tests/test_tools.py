"""Tests for the shared ``tools`` module."""

from ..tools import echo


def test_echo_tags_output():
    assert echo("hello") == "[shared] hello"
