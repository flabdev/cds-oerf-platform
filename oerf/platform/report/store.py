"""Versioned report objects. A published key is never replaced."""

from __future__ import annotations

import hashlib
from typing import Protocol

from oerf.platform.report.model import SECTIONS, ReportReceipt


class ReportStore(Protocol):
    def save(
        self,
        tenant_id: str,
        incident_id: str,
        body: bytes,
        extension: str,
        generated_at: str,
    ) -> ReportReceipt:
        """Write the next version and return its checksum."""


class MemoryReportStore:
    """In-memory bucket used by tests and by a handler with no bucket configured."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def save(
        self,
        tenant_id: str,
        incident_id: str,
        body: bytes,
        extension: str,
        generated_at: str,
    ) -> ReportReceipt:
        version = 1 + sum(
            1 for key in self.objects if key.startswith(f"reports/{tenant_id}/{incident_id}/")
        )
        key = f"reports/{tenant_id}/{incident_id}/v{version}.{extension}"
        if key in self.objects:
            raise FileExistsError(key)
        self.objects[key] = body
        return _receipt(version, key, body, generated_at)


class S3ReportStore:
    """Write-only publisher. Listing existing keys chooses the next version."""

    def __init__(self, client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def save(
        self,
        tenant_id: str,
        incident_id: str,
        body: bytes,
        extension: str,
        generated_at: str,
    ) -> ReportReceipt:
        prefix = f"reports/{tenant_id}/{incident_id}/"
        found = self._client.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
        version = len(found.get("Contents", [])) + 1
        key = f"{prefix}v{version}.{extension}"
        content_type = "application/pdf" if extension == "pdf" else "text/html"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
        return _receipt(version, key, body, generated_at)


def _receipt(version: int, key: str, body: bytes, generated_at: str) -> ReportReceipt:
    return ReportReceipt(
        report_version=version,
        s3_key=key,
        sha256=hashlib.sha256(body).hexdigest(),
        locked=True,
        generated_at=generated_at,
        sections=list(SECTIONS),
    )
