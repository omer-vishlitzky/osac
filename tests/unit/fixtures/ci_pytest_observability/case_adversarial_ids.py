"""Adversarial parameter ids: separators and brackets inside the payload.

pytest embeds parameter values (default ids) and custom id strings
verbatim into node ids, so ``::``, ``[``, ``]`` and Unicode can all
appear inside the parameter payload. The producer must hash the entire
payload from its first opening bracket; the native JUnit keeps the raw
ids.
"""

from __future__ import annotations

import pytest

from tests.e2e.core.observability.context import OperationContext, current_test_ref

CANARY = "EN-canary"


@pytest.fixture
def adversarial_resource() -> str:
    return "resource"


@pytest.mark.parametrize("value", [f"fe80::{CANARY}"])
def test_default_colon_id(value: str, adversarial_resource: str) -> None:
    assert isinstance(value, str)
    assert adversarial_resource


@pytest.mark.parametrize("value", ["tok-value"], ids=lambda v: f"TOK::{CANARY}")
def test_custom_colon_id(value: str) -> None:
    assert isinstance(value, str)


@pytest.mark.parametrize("value", ["brk-value"], ids=lambda v: f"{CANARY}[brk")
def test_custom_open_bracket_id(value: str) -> None:
    assert isinstance(value, str)


@pytest.mark.parametrize("value", ["close-value"], ids=lambda v: f"brk]{CANARY}")
def test_custom_close_bracket_id(value: str) -> None:
    assert isinstance(value, str)


@pytest.mark.parametrize("value", ["unicode-value"], ids=lambda v: f"{CANARY}-Ω-✓")
def test_unicode_id(value: str) -> None:
    assert isinstance(value, str)


@pytest.mark.parametrize("value", [f"{CANARY}"])
def test_operation_under_adversarial_id(value: str) -> None:
    with OperationContext(name="wait_for_adversarial", expected_condition="done", test=current_test_ref()) as op:
        op.progress(observed_state="working")


@pytest.mark.parametrize("value", ["plain-value"])
def test_plain_control(value: str) -> None:
    assert isinstance(value, str)
