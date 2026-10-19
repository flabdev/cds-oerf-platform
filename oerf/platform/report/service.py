"""Publish the HTML source document. PDF is optional and flag-gated."""

from __future__ import annotations

import os

from oerf.platform.report.model import ReportDocument, ReportReceipt
from oerf.platform.report.render import render_html
from oerf.platform.report.store import ReportStore


def publish_report(
    document: ReportDocument,
    store: ReportStore,
    *,
    pdf_backend: str | None = None,
) -> ReportReceipt:
    """Render HTML, optionally convert it, and store the next immutable version."""

    backend = pdf_backend if pdf_backend is not None else os.environ.get(
        "REPORT_PDF_BACKEND", "none"
    )
    html = render_html(document)
    body, extension = html.encode("utf-8"), "html"
    if backend == "weasyprint":
        rendered = _render_pdf(html)
        if rendered is not None:
            body, extension = rendered, "pdf"
    return store.save(
        document.tenant_id,
        document.incident_id,
        body,
        extension,
        document.generated_at,
    )


def _render_pdf(html: str) -> bytes | None:
    try:
        import weasyprint
    except ImportError:
        return None
    return weasyprint.HTML(string=html).write_pdf()
