"""Credential-free checks for the WhatsApp template definitions."""

import pytest

from scripts.whatsapp.templates import Template, definition, lint, load

TEMPLATES = load()


def test_catalog_has_the_twenty_page_20_templates():
    assert len(TEMPLATES) == 20
    assert len({t.name for t in TEMPLATES}) == 20


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_template_passes_lint(template):
    assert lint(template) == []


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_template_has_stop_footer(template):
    assert template.footer == "Reply STOP to opt out"


def test_lint_catches_leading_and_trailing_variables():
    bad = Template(name="oerf_x", header="H", body="{{1}} is late by {{2}}.", examples=["a", "b"])
    problems = lint(bad)
    assert "body starts with a variable" in problems
    assert any("ends with a variable" in p for p in problems)


def test_url_button_needs_base_url():
    t = next(t for t in TEMPLATES if t.needs_link)
    with pytest.raises(ValueError):
        definition(t, None)
    body = definition(t, "https://example.com")
    url = body["components"][-1]["buttons"][-1]
    assert url["url"].startswith("https://example.com/") and url["url"].endswith("{{1}}")


def test_definition_shape():
    body = definition(TEMPLATES[0], None)
    assert body["category"] == "UTILITY" and body["language"] == "en"
    types = [c["type"] for c in body["components"]]
    assert types == ["HEADER", "BODY", "FOOTER", "BUTTONS"]
    assert body["components"][1]["example"]["body_text"] == [TEMPLATES[0].examples]
