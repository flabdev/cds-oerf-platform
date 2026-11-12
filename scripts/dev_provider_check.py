"""Read-only check that the dev router can send the stage's WhatsApp templates.

Prints match or mismatch. It does not print the phone-number id, the WABA id,
or a template body, and it does not create or change anything.

``STAGE_URL_TEMPLATES`` in the output is the router's
``WHATSAPP_STAGE_URL_TEMPLATES`` value. Deploy sets that from the dev
environment variable of the same name. A warning is printed when it is true
while either dev URL-button copy is not APPROVED.

    python -m scripts.dev_provider_check --stage dev --profile <profile>
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from scripts._stage import REGION, remember_stage, resolve_stage
from scripts.whatsapp.templates import (
    DEV_URL_COPIES,
    dev_flow_templates,
    existing,
    load,
)

_PHONE_PREFIX = "phone-number-id-"
_PHONE_MARKER = ":phone-number-id/"


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Any = None,
) -> int:
    parser = argparse.ArgumentParser(description="Read-only dev WhatsApp provider check.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--stage", default=None)
    args = parser.parse_args(argv)
    target = remember_stage(resolve_stage(args.stage))
    if target.name != "dev":
        raise SystemExit("dev-provider-check only checks the dev stage")
    print(f"stage={target.name}")
    session = (session_factory or _session)(args.profile)
    failed = False
    phone_name = target.phone_number_id_parameter
    ssm = session.client("ssm", region_name=REGION)
    phone_id = _parameter_value(ssm, phone_name)
    if phone_id:
        print(f"PASS parameter {phone_name}")
    else:
        print(f"FAIL parameter {phone_name}")
        failed = True
    lambda_client = session.client("lambda", region_name=REGION)
    configuration = _function_configuration(lambda_client, target.router_function)
    role_arn = ""
    variables: dict[str, str] = {}
    if isinstance(configuration, dict):
        raw_role = configuration.get("Role")
        if isinstance(raw_role, str):
            role_arn = raw_role.strip()
        environment = configuration.get("Environment")
        raw_vars = environment.get("Variables") if isinstance(environment, dict) else None
        if isinstance(raw_vars, dict):
            variables = {
                key: value.strip()
                for key, value in raw_vars.items()
                if isinstance(key, str) and isinstance(value, str)
            }
    resources = _social_resources(session, role_arn) if role_arn else []
    star = any(resource == "*" or resource.endswith("/*") for resource in resources)
    scoped = bool(resources) and not star and all(_arn_phone_id(resource) for resource in resources)
    if phone_id and scoped:
        expected = _bare_phone_id(phone_id)
        found = {_arn_phone_id(resource) for resource in resources}
        if found == {expected}:
            print("PASS phone-id match")
        else:
            print("FAIL phone-id mismatch")
            failed = True
    elif phone_id:
        print("FAIL phone-id mismatch")
        failed = True
    if star:
        print("FAIL social-messaging resource=*")
        failed = True
    elif scoped:
        print("PASS social-messaging scoped")
    else:
        print("FAIL social-messaging scoped")
        failed = True
    statuses = _template_statuses(session, ssm, target.waba_parameter)
    if statuses is None:
        print(f"FAIL templates {target.waba_parameter}")
        failed = True
        statuses = {}
    else:
        for template in dev_flow_templates(load()):
            status = statuses.get(template.name, "NOT SUBMITTED")
            print(f"{template.name} {status}")
    flag = variables.get("WHATSAPP_STAGE_URL_TEMPLATES", "false").lower() == "true"
    print(f"STAGE_URL_TEMPLATES {'true' if flag else 'false'}")
    if flag and _dev_copies_pending(statuses):
        print("WARNING STAGE_URL_TEMPLATES is true while a dev copy is not APPROVED")
    return 1 if failed else 0


def _dev_copies_pending(statuses: dict[str, str]) -> bool:
    for _source, name in DEV_URL_COPIES:
        if statuses.get(name) != "APPROVED":
            return True
    return False


def _parameter_value(ssm: Any, name: str) -> str:
    try:
        found = ssm.get_parameter(Name=name)
    except Exception:
        return ""
    parameter = found.get("Parameter") if isinstance(found, dict) else None
    value = parameter.get("Value") if isinstance(parameter, dict) else ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _function_configuration(lambda_client: Any, name: str) -> dict[str, Any] | None:
    try:
        found = lambda_client.get_function_configuration(FunctionName=name)
    except Exception:
        return None
    return found if isinstance(found, dict) else None


def _social_resources(session: Any, role_arn: str) -> list[str]:
    role_name = role_arn.rsplit("/", 1)[-1]
    if not role_name:
        return []
    iam = session.client("iam", region_name=REGION)
    resources: list[str] = []
    for document in _policy_documents(iam, role_name):
        resources.extend(_statement_resources(document))
    return resources


def _policy_documents(iam: Any, role_name: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    marker = None
    while True:
        kwargs: dict[str, str] = {"RoleName": role_name}
        if marker:
            kwargs["Marker"] = marker
        try:
            page = iam.list_role_policies(**kwargs)
        except Exception:
            return documents
        for name in page.get("PolicyNames") or []:
            if not isinstance(name, str):
                continue
            try:
                found = iam.get_role_policy(RoleName=role_name, PolicyName=name)
            except Exception:
                continue
            raw_document = found.get("PolicyDocument") if isinstance(found, dict) else None
            document = _as_document(raw_document)
            if document is not None:
                documents.append(document)
        if not page.get("IsTruncated"):
            break
        marker = page.get("Marker")
        if not isinstance(marker, str) or not marker:
            break
    marker = None
    while True:
        kwargs = {"RoleName": role_name}
        if marker:
            kwargs["Marker"] = marker
        try:
            page = iam.list_attached_role_policies(**kwargs)
        except Exception:
            return documents
        for item in page.get("AttachedPolicies") or []:
            if not isinstance(item, dict):
                continue
            policy_arn = item.get("PolicyArn")
            document = _attached_document(iam, policy_arn)
            if document is not None:
                documents.append(document)
        if not page.get("IsTruncated"):
            break
        marker = page.get("Marker")
        if not isinstance(marker, str) or not marker:
            break
    return documents


def _attached_document(iam: Any, policy_arn: object) -> dict[str, Any] | None:
    if not isinstance(policy_arn, str) or not policy_arn:
        return None
    try:
        meta = iam.get_policy(PolicyArn=policy_arn)
        version = meta.get("Policy", {}).get("DefaultVersionId")
        if not isinstance(version, str) or not version:
            return None
        loaded = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version)
    except Exception:
        return None
    version_body = loaded.get("PolicyVersion") if isinstance(loaded, dict) else None
    raw = version_body.get("Document") if isinstance(version_body, dict) else None
    return _as_document(raw)


def _as_document(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, dict):
        return raw
    return None


def _statement_resources(document: dict[str, Any]) -> list[str]:
    found: list[str] = []
    statements = document.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect", "Allow") != "Allow":
            continue
        actions = statement.get("Action") or []
        if isinstance(actions, str):
            actions = [actions]
        social = [
            action
            for action in actions
            if isinstance(action, str) and action.startswith("social-messaging:")
        ]
        if not social:
            continue
        resources = statement.get("Resource") or []
        if isinstance(resources, str):
            resources = [resources]
        for resource in resources:
            if isinstance(resource, str) and resource.strip():
                found.append(resource.strip())
    return found


def _bare_phone_id(value: str) -> str:
    text = value.strip()
    if text.startswith(_PHONE_PREFIX):
        return text[len(_PHONE_PREFIX) :]
    return text


def _arn_phone_id(resource: str) -> str:
    if _PHONE_MARKER not in resource or resource == "*" or resource.endswith("/*"):
        return ""
    return resource.split(_PHONE_MARKER, 1)[1].strip()


def _template_statuses(session: Any, ssm: Any, parameter_name: str) -> dict[str, str] | None:
    waba_id = _parameter_value(ssm, parameter_name)
    if not waba_id.startswith("waba-"):
        return None
    client = session.client("socialmessaging", region_name=REGION)
    try:
        found = existing(client, waba_id)
    except Exception:
        return None
    return {
        key.split("|", 1)[0]: str(item.get("templateStatus") or "")
        for key, item in found.items()
        if isinstance(item, dict)
    }


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=REGION)


if __name__ == "__main__":
    raise SystemExit(main())
