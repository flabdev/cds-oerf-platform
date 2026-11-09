"""The report port the ``GenerateReport`` step publishes through.

The reporting implementation is ``oerf.platform.report.generator``, which reads
the canonical incident row plus its ``EVT#`` and ``READ#`` children from the
incident table and writes an immutable object to the report bucket. That module
builds its own boto3 clients, so the step reaches it through this port instead of
importing it directly: a handler that is given no reporter fails loudly rather
than reporting success without publishing anything.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ReportPublishError(RuntimeError):
    """The reporter could not publish the report for this incident."""


@runtime_checkable
class ReportPublisher(Protocol):
    """Publish one incident report and return its receipt."""

    def publish(self, *, tenant_id: str, incident_id: str) -> dict[str, Any]:
        """Return the published receipt, or raise :class:`ReportPublishError`."""


class GeneratorReportPublisher:
    """Publish through ``oerf.platform.report.generator.handler``."""

    def __init__(self, context: Any = None) -> None:
        self._context = context

    def publish(self, *, tenant_id: str, incident_id: str) -> dict[str, Any]:
        from oerf.platform.report.generator import handler as publish

        result = publish({"tenant_id": tenant_id, "incident_id": incident_id}, self._context)
        if not isinstance(result, dict) or result.get("success") is not True:
            reason = result.get("error") if isinstance(result, dict) else "no result"
            raise ReportPublishError(
                f"report generation failed for incident {incident_id}: {reason}"
            )
        return result
