"""TokenGovernor.__getattr__ must proxy safely and never recurse.

Regression: a second ``__getattr__`` definition silently replaced the
recursion guard. A self-referential wrap or a copy then recursed until
RecursionError.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from blue_engine.core.llm.tokens import TokenGovernor


def test_attribute_access_is_proxied_to_the_client():
    gov = TokenGovernor(SimpleNamespace(backends=["ollama"]))
    assert gov.backends == ["ollama"]


def test_self_referential_wrap_fails_closed_not_recursion():
    gov = TokenGovernor(SimpleNamespace(backends=[]))
    gov.client = gov                       # the wrap the docstring warns about
    with pytest.raises(AttributeError):
        gov.not_a_real_attribute


def test_copy_does_not_recurse():
    gov = TokenGovernor(SimpleNamespace(backends=[]))
    clone = copy.copy(gov)                 # builds the object without __init__
    assert isinstance(clone, TokenGovernor)
