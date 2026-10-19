"""Submit and track OERF WhatsApp templates in AWS End User Messaging Social.

Operator tool. Never runs in Lambda or CI. Default action is a dry run.

    python -m scripts.whatsapp.templates lint
    python -m scripts.whatsapp.templates plan  --dashboard-base-url https://example.com/
    python -m scripts.whatsapp.templates submit --profile cds-oerf-poc [--only NAME ...] [--yes]
    python -m scripts.whatsapp.templates status --profile cds-oerf-poc

The WABA id is read from --waba-id or SSM /oerf/whatsapp/waba-id (us-east-1).
Templates with a URL button need --dashboard-base-url; without it they are skipped,
because Meta fixes the link domain at approval time.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_FILE = Path(__file__).with_name("templates.json")
REGION = "us-east-1"
WABA_PARAM = "/oerf/whatsapp/waba-id"

NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
VAR_RE = re.compile(r"\{\{(\d+)\}\}")
MAX_HEADER, MAX_BODY, MAX_FOOTER, MAX_BUTTON = 60, 1024, 60, 20
MAX_QUICK_REPLY, MAX_URL_BUTTONS = 10, 2


@dataclass
class Template:
    name: str
    header: str
    body: str
    examples: list[str]
    buttons: list[dict[str, Any]] = field(default_factory=list)
    category: str = "UTILITY"
    language: str = "en"
    footer: str = ""

    @property
    def needs_link(self) -> bool:
        return any(b["type"] == "URL" for b in self.buttons)


def load(path: Path = DEFAULT_FILE) -> list[Template]:
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    return [Template(**{**defaults, **raw}) for raw in data["templates"]]


def lint(t: Template) -> list[str]:
    """Return problems likely to make Meta reject the template (empty list = OK)."""
    errors: list[str] = []
    if not NAME_RE.match(t.name):
        errors.append("name must be lowercase letters, digits and underscores")
    if not t.name.startswith("oerf_"):
        errors.append("name must start with oerf_ (Notion page 20)")
    if t.category != "UTILITY":
        errors.append("category must be UTILITY")
    if len(t.header) > MAX_HEADER:
        errors.append(f"header longer than {MAX_HEADER} characters")
    if VAR_RE.search(t.header):
        errors.append("header must not contain variables")
    if len(t.body) > MAX_BODY:
        errors.append(f"body longer than {MAX_BODY} characters")
    if t.footer and len(t.footer) > MAX_FOOTER:
        errors.append(f"footer longer than {MAX_FOOTER} characters")

    numbers = [int(n) for n in VAR_RE.findall(t.body)]
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append("body variables must be {{1}}, {{2}} ... in order, each used once")
    if len(t.examples) != len(numbers):
        errors.append(f"{len(numbers)} body variables but {len(t.examples)} sample values")
    stripped = t.body.strip()
    if VAR_RE.match(stripped):
        errors.append("body starts with a variable")
    tail = VAR_RE.split(stripped)[-1] if numbers else stripped
    if numbers and not re.search(r"[A-Za-z]", tail):
        errors.append("body ends with a variable (only punctuation after the last one)")
    fixed_words = len(re.findall(r"[A-Za-z]+", VAR_RE.sub("", t.body)))
    if numbers and fixed_words < 3 * len(numbers):
        errors.append("too few fixed words for the number of variables")

    quick = [b for b in t.buttons if b["type"] == "QUICK_REPLY"]
    urls = [b for b in t.buttons if b["type"] == "URL"]
    if len(quick) > MAX_QUICK_REPLY:
        errors.append(f"more than {MAX_QUICK_REPLY} quick-reply buttons")
    if len(urls) > MAX_URL_BUTTONS:
        errors.append(f"more than {MAX_URL_BUTTONS} URL buttons")
    for b in t.buttons:
        if b["type"] not in {"QUICK_REPLY", "URL"}:
            errors.append(f"unsupported button type {b['type']}")
        if len(b["text"]) > MAX_BUTTON:
            errors.append(f"button '{b['text']}' longer than {MAX_BUTTON} characters")
        if b["type"] == "URL" and not b.get("path", "").endswith("{{1}}"):
            errors.append("URL button path must end with {{1}}")
    labels = [b["text"].lower() for b in t.buttons]
    if len(labels) != len(set(labels)):
        errors.append("duplicate button labels")
    if re.search(r"\+?\d[\d ]{8,}\d", VAR_RE.sub("", t.body)):
        errors.append("body text contains a phone number; use a variable")
    return errors


def definition(t: Template, base_url: str | None) -> dict[str, Any]:
    """Build the Meta template JSON that End User Messaging Social forwards to Meta."""
    components: list[dict[str, Any]] = [
        {"type": "HEADER", "format": "TEXT", "text": t.header},
        {"type": "BODY", "text": t.body, "example": {"body_text": [t.examples]}},
    ]
    if t.footer:
        components.append({"type": "FOOTER", "text": t.footer})
    if t.buttons:
        buttons: list[dict[str, Any]] = []
        for b in t.buttons:
            if b["type"] == "QUICK_REPLY":
                buttons.append({"type": "QUICK_REPLY", "text": b["text"]})
            else:
                if not base_url:
                    raise ValueError(f"{t.name} needs --dashboard-base-url")
                root = base_url if base_url.endswith("/") else base_url + "/"
                buttons.append(
                    {
                        "type": "URL",
                        "text": b["text"],
                        "url": root + b["path"],
                        "example": [root + b["example"]],
                    }
                )
        components.append({"type": "BUTTONS", "buttons": buttons})
    return {
        "name": t.name,
        "language": t.language,
        "category": t.category,
        "components": components,
    }


# --- AWS helpers (imported lazily so lint/plan work without boto3 or credentials) ---


def _client(profile: str | None, service: str):
    import boto3

    return boto3.Session(profile_name=profile, region_name=REGION).client(service)


def _waba_id(args: argparse.Namespace) -> str:
    if args.waba_id:
        return args.waba_id
    value = _client(args.profile, "ssm").get_parameter(Name=WABA_PARAM)["Parameter"]["Value"]
    if not value.startswith("waba-"):
        sys.exit(f"{WABA_PARAM} does not look like an AWS WABA id (waba-...)")
    return value


def existing(client, waba_id: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    token = None
    while True:
        kwargs = {"id": waba_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = client.list_whatsapp_message_templates(**kwargs)
        for item in page.get("templates", []):
            found[f"{item['templateName']}|{item.get('templateLanguage', '')}"] = item
        token = page.get("nextToken")
        if not token:
            return found


def _create(client, waba_id: str, body: dict[str, Any], attempts: int = 5) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    for attempt in range(attempts):
        try:
            return client.create_whatsapp_message_template(
                id=waba_id, templateDefinition=json.dumps(body).encode("utf-8")
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ThrottledRequestException" or (
                attempt == attempts - 1
            ):
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


# --- commands ---


def cmd_lint(templates: list[Template]) -> int:
    bad = 0
    for t in templates:
        for problem in lint(t):
            bad += 1
            print(f"FAIL {t.name}: {problem}")
    print(f"{len(templates)} templates checked, {bad} problem(s)")
    return 1 if bad else 0


def cmd_plan(templates: list[Template], base_url: str | None) -> int:
    for t in templates:
        if t.needs_link and not base_url:
            print(f"# SKIP {t.name}: needs --dashboard-base-url")
            continue
        print(json.dumps(definition(t, base_url), ensure_ascii=False, indent=2))
    return 0


def cmd_submit(args: argparse.Namespace, templates: list[Template]) -> int:
    from botocore.exceptions import ClientError

    if cmd_lint(templates):
        print("Fix lint problems before submitting.")
        return 1
    client = _client(args.profile, "socialmessaging")
    waba_id = _waba_id(args)
    have = existing(client, waba_id)
    todo = []
    for t in templates:
        if f"{t.name}|{t.language}" in have:
            print(f"EXISTS {t.name}: {have[f'{t.name}|{t.language}']['templateStatus']}")
        elif t.needs_link and not args.dashboard_base_url:
            print(f"SKIP   {t.name}: needs --dashboard-base-url")
        else:
            todo.append(t)
    if not todo:
        print("Nothing to submit.")
        return 0
    names = ", ".join(t.name for t in todo)
    print(f"\nWill submit {len(todo)} template(s) to {waba_id}: {names}")
    if not args.yes:
        print("Dry run. Re-run with --yes to submit.")
        return 0
    failures = 0
    for t in todo:
        try:
            result = _create(client, waba_id, definition(t, args.dashboard_base_url))
            status, meta_id = result.get("templateStatus"), result.get("metaTemplateId")
            print(f"SENT   {t.name}: {status} (meta id {meta_id})")
        except ClientError as exc:
            failures += 1
            err = exc.response["Error"]
            print(f"ERROR  {t.name}: {err['Code']}: {err.get('Message', '')}")
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace, templates: list[Template]) -> int:
    client = _client(args.profile, "socialmessaging")
    have = existing(client, _waba_id(args))
    print(f"{'template':34} {'status':12} quality")
    for t in templates:
        item = have.get(f"{t.name}|{t.language}")
        status = item["templateStatus"] if item else "NOT SUBMITTED"
        quality = item.get("templateQualityScore", "") if item else ""
        print(f"{t.name:34} {status:12} {quality}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["lint", "plan", "submit", "status"])
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--only", nargs="*", help="limit to these template names")
    parser.add_argument("--profile", help="AWS profile for the cds-oerf-poc account")
    parser.add_argument("--waba-id", help=f"AWS WABA id; default reads SSM {WABA_PARAM}")
    parser.add_argument("--dashboard-base-url", help="fixed link root for URL buttons")
    parser.add_argument("--yes", action="store_true", help="actually submit (default: dry run)")
    args = parser.parse_args(argv)

    templates = load(args.file)
    if args.only:
        unknown = set(args.only) - {t.name for t in templates}
        if unknown:
            parser.error(f"unknown template(s): {', '.join(sorted(unknown))}")
        templates = [t for t in templates if t.name in args.only]

    if args.command == "lint":
        return cmd_lint(templates)
    if args.command == "plan":
        return cmd_plan(templates, args.dashboard_base_url)
    if args.command == "submit":
        return cmd_submit(args, templates)
    return cmd_status(args, templates)


if __name__ == "__main__":
    sys.exit(main())
