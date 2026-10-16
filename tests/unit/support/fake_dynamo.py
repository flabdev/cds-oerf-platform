"""In-memory stand-in for the low-level DynamoDB client operations these adapters use."""

from __future__ import annotations

import threading
from typing import Any

from oerf.platform.storage.dynamo import client_error

Item = dict[str, dict[str, str]]


class FakeDynamoClient:
    """Low-level DynamoDB operations used by the incident, dedupe, and debounce adapters."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], Item] = {}
        self._guard = threading.Lock()
        self.transactions: list[list[str]] = []
        self.writes = 0

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        with self._guard:
            self._put(kwargs)
        return {}

    def get_item(self, *, TableName: str, Key: Item, **_: Any) -> dict[str, Any]:
        with self._guard:
            item = self._items.get((TableName, Key["pk"]["S"], Key["sk"]["S"]))
        if item is None:
            return {}
        return {"Item": _copy(item)}

    def delete_item(self, *, TableName: str, Key: Item, **kwargs: Any) -> dict[str, Any]:
        with self._guard:
            self._delete(TableName, Key, kwargs)
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        with self._guard:
            return self._update(kwargs)

    def scan(self, *, TableName: str, **_: Any) -> dict[str, Any]:
        with self._guard:
            items = [
                _copy(item)
                for (table, _, _), item in self._items.items()
                if table == TableName
            ]
        return {"Items": items}

    def query(
        self,
        *,
        TableName: str,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict[str, dict[str, str]],
        **_: Any,
    ) -> dict[str, Any]:
        if ":pk" not in KeyConditionExpression or ":prefix" not in KeyConditionExpression:
            raise client_error("ValidationException")
        pk = ExpressionAttributeValues[":pk"]["S"]
        prefix = ExpressionAttributeValues[":prefix"]["S"]
        with self._guard:
            found = [
                _copy(item)
                for (table, item_pk, item_sk), item in self._items.items()
                if table == TableName and item_pk == pk and item_sk.startswith(prefix)
            ]
        found.sort(key=lambda item: item["sk"]["S"])
        return {"Items": found}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        with self._guard:
            reasons: list[dict[str, str]] = []
            failed = False
            staged: list[tuple[str, dict[str, Any]]] = []
            for entry in TransactItems:
                operation, body = next(iter(entry.items()))
                ok = self._allowed(operation, body)
                reasons.append({"Code": "None" if ok else "ConditionalCheckFailed"})
                failed = failed or not ok
                staged.append((operation, body))
            if failed:
                raise client_error("TransactionCanceledException", reasons)
            for operation, body in staged:
                self._apply(operation, body)
            self.transactions.append([next(iter(entry)) for entry in TransactItems])
        return {}

    def _put(self, body: dict[str, Any]) -> None:
        item = _copy(body["Item"])
        storage_key = (body["TableName"], item["pk"]["S"], item["sk"]["S"])
        current = self._items.get(storage_key)
        if not _condition_holds(body, current):
            raise client_error("ConditionalCheckFailedException")
        self._items[storage_key] = item
        self.writes += 1

    def _delete(self, table: str, key: Item, body: dict[str, Any]) -> None:
        storage_key = (table, key["pk"]["S"], key["sk"]["S"])
        current = self._items.get(storage_key)
        if not _condition_holds(body, current):
            raise client_error("ConditionalCheckFailedException")
        self._items.pop(storage_key, None)
        self.writes += 1

    def _update(self, body: dict[str, Any]) -> dict[str, Any]:
        key = body["Key"]
        storage_key = (body["TableName"], key["pk"]["S"], key["sk"]["S"])
        current = self._items.get(storage_key)
        if not _condition_holds(body, current):
            raise client_error("ConditionalCheckFailedException")
        item = _copy(current) if current is not None else _copy(key)
        updated = _apply_set(body, item)
        self._items[storage_key] = item
        self.writes += 1
        if body.get("ReturnValues") == "UPDATED_NEW":
            return {"Attributes": updated}
        return {}

    def _allowed(self, operation: str, body: dict[str, Any]) -> bool:
        if operation == "Put":
            item = body["Item"]
            current = self._items.get((body["TableName"], item["pk"]["S"], item["sk"]["S"]))
            return _condition_holds(body, current)
        if operation == "Delete":
            key = body["Key"]
            current = self._items.get((body["TableName"], key["pk"]["S"], key["sk"]["S"]))
            return _condition_holds(body, current)
        if operation == "Update":
            key = body["Key"]
            current = self._items.get((body["TableName"], key["pk"]["S"], key["sk"]["S"]))
            return _condition_holds(body, current)
        raise AssertionError(operation)

    def _apply(self, operation: str, body: dict[str, Any]) -> None:
        if operation == "Put":
            self._put(body)
        elif operation == "Delete":
            self._delete(body["TableName"], body["Key"], body)
        elif operation == "Update":
            self._update(body)
        else:
            raise AssertionError(operation)


def _copy(item: Item) -> Item:
    return {name: dict(value) for name, value in item.items()}


def _condition_holds(body: dict[str, Any], item: Item | None) -> bool:
    expression = body.get("ConditionExpression")
    if not expression:
        return True
    names = body.get("ExpressionAttributeNames") or {}
    values = body.get("ExpressionAttributeValues") or {}
    return any(_all_terms(part, names, values, item) for part in _split_top(expression, " OR "))


def _all_terms(expression: str, names: dict[str, str], values: Item, item: Item | None) -> bool:
    return all(
        _term(part.strip(), names, values, item) for part in _split_top(expression, " AND ")
    )


def _term(term: str, names: dict[str, str], values: Item, item: Item | None) -> bool:
    if term.startswith("attribute_not_exists("):
        return _attribute(term, names) not in (item or {})
    if term.startswith("attribute_exists("):
        return _attribute(term, names) in (item or {})
    for operator in ("<=", ">=", ">", "<", "="):
        if operator in term:
            left, right = term.split(operator, 1)
            left_value = _operand(left.strip(), names, values, item)
            right_value = _operand(right.strip(), names, values, item)
            if left_value is None or right_value is None:
                return False
            return _compare(left_value, right_value, operator)
    raise AssertionError(term)


def _attribute(term: str, names: dict[str, str]) -> str:
    inner = term[term.index("(") + 1 : -1].strip()
    return names.get(inner, inner)


def _operand(token: str, names: dict[str, str], values: Item, item: Item | None) -> object | None:
    if token.startswith(":"):
        return _unwrap(values[token])
    name = names.get(token, token)
    if item is None or name not in item:
        return None
    return _unwrap(item[name])


def _unwrap(attribute: dict[str, str]) -> object:
    if "N" in attribute:
        return int(attribute["N"])
    return attribute["S"]


def _compare(left: object, right: object, operator: str) -> bool:
    if operator == "=":
        return left == right
    if operator == ">":
        return left > right
    if operator == "<":
        return left < right
    if operator == ">=":
        return left >= right
    return left <= right


def _apply_set(body: dict[str, Any], item: Item) -> Item:
    names = body.get("ExpressionAttributeNames") or {}
    values = body.get("ExpressionAttributeValues") or {}
    updated: Item = {}
    expression = body["UpdateExpression"].removeprefix("SET ")
    for assignment in _split_top(expression, ","):
        target, raw = assignment.split("=", 1)
        name = names.get(target.strip(), target.strip())
        value = _set_value(raw.strip(), names, values, item)
        item[name] = value
        updated[name] = dict(value)
    return updated


def _set_value(expression: str, names: dict[str, str], values: Item, item: Item) -> dict[str, str]:
    if expression.startswith("if_not_exists("):
        inner, added = expression.split(") + ", 1)
        attr_token, base_token = inner[len("if_not_exists(") :].split(",", 1)
        name = names.get(attr_token.strip(), attr_token.strip())
        current = int(item[name]["N"]) if name in item else int(values[base_token.strip()]["N"])
        return {"N": str(current + int(values[added.strip()]["N"]))}
    if " + " in expression:
        attr_token, added = expression.split(" + ", 1)
        name = names.get(attr_token.strip(), attr_token.strip())
        return {"N": str(int(item[name]["N"]) + int(values[added.strip()]["N"]))}
    return dict(values[expression])


def _split_top(expression: str, separator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and expression.startswith(separator, index):
            parts.append(expression[start:index])
            index += len(separator)
            start = index
            continue
        index += 1
    parts.append(expression[start:])
    return parts
