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


def test_dev_copies_keep_the_original_text_and_take_the_desk_at_submit_time():
    from pathlib import Path

    from scripts.whatsapp.templates import DEV_URL_COPIES, dev_copies

    catalog = Path("scripts/whatsapp/templates.json").read_text(encoding="utf-8")
    assert "oerf_dev_ops_escalation_options" not in catalog
    assert "oerf_dev_qa_report_ready" not in catalog
    assert "cloudfront.net" not in catalog
    copies = dev_copies(TEMPLATES)
    originals = {item.name: item for item in TEMPLATES}
    assert [item.name for item in copies] == [name for _source, name in DEV_URL_COPIES]
    for source_name, copy in zip((source for source, _name in DEV_URL_COPIES), copies, strict=True):
        source = originals[source_name]
        assert lint(copy) == []
        assert copy.header == source.header
        assert copy.body == source.body
        assert copy.footer == source.footer
        assert copy.category == source.category
        assert copy.buttons == source.buttons
        built = definition(copy, "https://dev.example.test")
        button = built["components"][-1]["buttons"][-1]
        assert button["url"].startswith("https://dev.example.test/")
        assert button["url"].endswith("{{1}}")
        assert button["example"] == [button["url"].replace("{{1}}", "INC-001")]
        assert "main.example.test" not in button["url"]
        assert "cloudfront.net" not in button["url"]


def test_dev_submit_creates_only_the_two_copies(monkeypatch: pytest.MonkeyPatch):
    import json

    from scripts.whatsapp import templates as templates_module

    created: list[dict] = []

    class _Social:
        def list_whatsapp_message_templates(self, **_kwargs: object) -> dict:
            return {"templates": []}

        def create_whatsapp_message_template(self, **kwargs: object) -> dict:
            raw = kwargs["templateDefinition"]
            assert isinstance(raw, bytes)
            created.append(json.loads(raw.decode()))
            return {"templateStatus": "PENDING", "metaTemplateId": "meta-1"}

    monkeypatch.setattr(templates_module, "_client", lambda _profile, service: _Social())
    code = templates_module.main(
        [
            "submit",
            "--stage",
            "dev",
            "--yes",
            "--profile",
            "p",
            "--waba-id",
            "waba-0000000000000000",
            "--dashboard-base-url",
            "https://dev.example.test",
        ]
    )
    assert code == 0
    names = [item["name"] for item in created]
    assert names == ["oerf_dev_ops_escalation_options", "oerf_dev_qa_report_ready"]
    blob = json.dumps(created)
    assert "https://dev.example.test/" in blob
    assert "oerf_ops_escalation_options" not in names
    assert "cloudfront.net" not in blob


def test_main_submit_does_not_create_dev_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from scripts.whatsapp import templates as templates_module

    created: list[str] = []

    class _Social:
        def list_whatsapp_message_templates(self, **_kwargs: object) -> dict:
            return {"templates": []}

        def create_whatsapp_message_template(self, **kwargs: object) -> dict:
            raw = kwargs["templateDefinition"]
            assert isinstance(raw, bytes)
            created.append(json.loads(raw.decode())["name"])
            return {"templateStatus": "PENDING", "metaTemplateId": "meta-1"}

    monkeypatch.setenv("ALLOW_MAIN", "1")
    monkeypatch.setattr(templates_module, "_client", lambda _profile, service: _Social())
    code = templates_module.main(
        [
            "submit",
            "--stage",
            "main",
            "--yes",
            "--profile",
            "p",
            "--waba-id",
            "waba-0000000000000000",
            "--only",
            "oerf_driver_checkin",
            "--dashboard-base-url",
            "https://main.example.test",
        ]
    )
    assert code == 0
    assert created == ["oerf_driver_checkin"]


def test_dev_status_lists_copies_and_shared_names_without_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.whatsapp import templates as templates_module

    class _Social:
        def list_whatsapp_message_templates(self, **_kwargs: object) -> dict:
            return {
                "templates": [
                    {
                        "templateName": "oerf_dev_qa_report_ready",
                        "templateLanguage": "en",
                        "templateStatus": "APPROVED",
                    }
                ]
            }

    monkeypatch.setattr(templates_module, "_client", lambda _profile, service: _Social())
    code = templates_module.main(
        [
            "status",
            "--stage",
            "dev",
            "--profile",
            "p",
            "--waba-id",
            "waba-0000000000000000",
        ]
    )
    text = capsys.readouterr().out
    assert code == 0
    assert "oerf_dev_ops_escalation_options" in text
    assert "NOT SUBMITTED" in text
    assert "oerf_dev_qa_report_ready" in text
    assert "APPROVED" in text
    assert "oerf_driver_checkin" in text
