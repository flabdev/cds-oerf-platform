"""Account conditions on service invokes and queue policies.

API Gateway and EventBridge permissions name a source ARN. Queue allows that
already name a source ARN get the same account check. A transport deny is left
alone.
"""

from __future__ import annotations

from aws_cdk import Stack, aws_lambda as lambda_, aws_sqs as sqs
from constructs import Construct

_INVOKERS = frozenset({"apigateway.amazonaws.com", "events.amazonaws.com"})


def pin_source_account(scope: Construct) -> None:
    """Require this stack's account on those permissions and queue allows."""

    stack = Stack.of(scope)
    account = stack.account
    for child in scope.node.find_all():
        if isinstance(child, lambda_.CfnPermission) and child.principal in _INVOKERS:
            child.source_account = account
        elif isinstance(child, sqs.CfnQueuePolicy):
            _pin_queue_policy(stack, child, account)


def _pin_queue_policy(stack: Stack, policy: sqs.CfnQueuePolicy, account: str) -> None:
    document = stack.resolve(policy.policy_document)
    if not isinstance(document, dict):
        return
    changed = False
    for statement in document.get("Statement", []):
        if not isinstance(statement, dict):
            continue
        condition = statement.get("Condition", {})
        arn_equals = condition.get("ArnEquals", {}) if isinstance(condition, dict) else {}
        if not isinstance(arn_equals, dict) or "aws:SourceArn" not in arn_equals:
            continue
        equals = statement.setdefault("Condition", {}).setdefault("StringEquals", {})
        if equals.get("aws:SourceAccount") == account:
            continue
        equals["aws:SourceAccount"] = account
        changed = True
    if changed:
        policy.add_override("Properties.PolicyDocument", document)
