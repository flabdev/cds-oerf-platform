"""Trusted device registry on the routing table.

Ingest reads ``DEVICE#{device_id}`` / ``META`` and does not write it. The
tenant and shipment on that row are the only identity a temperature event may use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from oerf.platform.storage.dynamo import device_pk, key, required_text, s

_SEED_ROOT = Path(__file__).resolve().parents[3] / "sim" / "seed"


def register_device(
    client: Any,
    table_name: str,
    *,
    device_id: str,
    tenant_id: str,
    shipment_id: str,
    asset_id: str,
) -> None:
    """Put one device row. A later registration for the same id replaces it."""

    device = required_text(device_id, "device_id")
    tenant = required_text(tenant_id, "tenant_id")
    shipment = required_text(shipment_id, "shipment_id")
    asset = required_text(asset_id, "asset_id")
    client.put_item(
        TableName=table_name,
        Item={
            **key(device_pk(device), "META"),
            "device_id": s(device),
            "tenant_id": s(tenant),
            "shipment_id": s(shipment),
            "asset_id": s(asset),
        },
    )


def expected_demo_devices(root: Path | None = None) -> dict[str, tuple[str, str]]:
    """Device id to ``(tenant_id, shipment_id)`` from ``sim/seed/*/shipments.json``."""

    mapping: dict[str, tuple[str, str]] = {}
    seed_root = root or _SEED_ROOT
    for path in sorted(seed_root.glob("*/shipments.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            device = str(row.get("device") or "").strip()
            if not device:
                continue
            mapping[device] = (str(row["tenant_id"]), str(row["shipment_id"]))
    return mapping


def verify_demo_devices(client: Any, table_name: str, root: Path | None = None) -> list[str]:
    """Return device ids from the seed files that are missing or mismatched.

    Empty means every seeded ``DEVICE#`` row is present with the expected tenant
    and shipment. Call this after a deploy with ``demo:seedDevices=true``.
    """

    from oerf.platform.ingest.handler import DynamoDeviceResolver

    expected = expected_demo_devices(root)
    resolver = DynamoDeviceResolver(client, table_name)
    missing: list[str] = []
    for device_id, wanted in expected.items():
        found = resolver.resolve(device_id)
        if found != wanted:
            missing.append(device_id)
    return missing


def seed_demo_devices(client: Any, table_name: str, root: Path | None = None) -> list[str]:
    """Register the device on each shipment file under ``sim/seed``.

    ``device`` is the device id and ``truck`` is the asset. Returns the device ids written.
    """

    seed_root = root or _SEED_ROOT
    written: list[str] = []
    for path in sorted(seed_root.glob("*/shipments.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            device = str(row.get("device") or "").strip()
            if not device:
                continue
            register_device(
                client,
                table_name,
                device_id=device,
                tenant_id=str(row["tenant_id"]),
                shipment_id=str(row["shipment_id"]),
                asset_id=str(row.get("truck") or row["shipment_id"]),
            )
            written.append(device)
    return written


def seed_handler(event: dict[str, Any], _context: Any) -> dict[str, str]:
    """CloudFormation custom-resource entry point for demo device rows."""
    physical_id = str(event.get("PhysicalResourceId") or "oerf-demo-device-seed")
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": physical_id}
    table_name = os.environ.get("ROUTING_TABLE_NAME", "").strip()
    if not table_name:
        raise RuntimeError("ROUTING_TABLE_NAME is required")
    import boto3

    seed_demo_devices(boto3.client("dynamodb"), table_name)
    return {"PhysicalResourceId": physical_id}
