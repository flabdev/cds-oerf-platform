"""Versioned report objects. A published key is never replaced."""

from __future__ import annotations

import hashlib
import threading
from typing import Any, Protocol

from botocore.exceptions import ClientError

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
        self._guard = threading.Lock()

    def save(
        self,
        tenant_id: str,
        incident_id: str,
        body: bytes,
        extension: str,
        generated_at: str,
    ) -> ReportReceipt:
        prefix = f"reports/{tenant_id}/{incident_id}/"
        with self._guard:
            version = 1 + sum(1 for key in self.objects if key.startswith(prefix))
            key = f"{prefix}v{version}.{extension}"
            if key in self.objects:
                raise FileExistsError(key)
            self.objects[key] = body
        return _receipt(version, key, body, generated_at)


class S3ReportStore:
    """Write-only publisher. A published key is created only when it is absent."""

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
        version = _highest_version(self._listed_keys(prefix)) + 1
        content_type = "application/pdf" if extension == "pdf" else "text/html"
        for _ in range(32):
            key = f"{prefix}v{version}.{extension}"
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {
                    "PreconditionFailed",
                    "ConditionalRequestConflict",
                }:
                    raise
                version += 1
                continue
            return _receipt(version, key, body, generated_at)
        raise RuntimeError(f"report version was not allocated for {prefix}")

    def _listed_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            args: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token is not None:
                args["ContinuationToken"] = token
            page = self._client.list_objects_v2(**args)
            keys.extend(
                item["Key"]
                for item in page.get("Contents", [])
                if isinstance(item.get("Key"), str)
            )
            if not page.get("IsTruncated"):
                return keys
            token = page.get("NextContinuationToken")
            if not isinstance(token, str) or not token:
                return keys


def _highest_version(keys: list[str]) -> int:
    highest = 0
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        if not name.startswith("v") or "." not in name:
            continue
        number = name[1:].split(".", 1)[0]
        if number.isdigit():
            highest = max(highest, int(number))
    return highest


def _receipt(version: int, key: str, body: bytes, generated_at: str) -> ReportReceipt:
    return ReportReceipt(
        report_version=version,
        s3_key=key,
        sha256=hashlib.sha256(body).hexdigest(),
        locked=True,
        generated_at=generated_at,
        sections=list(SECTIONS),
    )
