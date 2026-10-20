"""Consultas aditivas privadas: tipadas, acotadas y sin secciones locales."""

import pytest
from pydantic import ValidationError

from dyndnsarr_shared.control import DiagnosticPage, EffectiveConfig


def test_configuration_rejects_local_sections_and_unknown_fields():
    effective = EffectiveConfig(version=0, hash=None, content={"records": []})
    assert effective.content.records == []
    for content in [{"providers": {}}, {"hub": {}}, {"password": "canary"}]:
        with pytest.raises(ValidationError):
            EffectiveConfig(version=0, hash=None, content=content)


def test_diagnostic_page_is_bounded_and_disallows_unknown_fields():
    with pytest.raises(ValidationError):
        DiagnosticPage(observations=[{}] * 101)
    with pytest.raises(ValidationError):
        DiagnosticPage(observations=[], token="secret")
