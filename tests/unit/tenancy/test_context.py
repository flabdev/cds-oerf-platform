"""Tests for tenant context validation."""

import pytest
from pydantic import ValidationError

from oerf.platform.tenancy.context import TenantContext, get_tenant_context


def test_valid_tenant_id_is_represented() -> None:
    context = TenantContext(tenant_id="T1")

    assert context.tenant_id == "T1"


def test_get_tenant_context_reads_mapping() -> None:
    context = get_tenant_context({"tenant_id": "T1"})

    assert context == TenantContext(tenant_id="T1")


def test_missing_tenant_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="tenant_id"):
        get_tenant_context({})


def test_null_tenant_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="tenant_id"):
        get_tenant_context({"tenant_id": None})


def test_empty_tenant_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        TenantContext(tenant_id="")


def test_blank_tenant_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        get_tenant_context({"tenant_id": "   "})


def test_context_round_trips_through_json() -> None:
    context = TenantContext(tenant_id="t1")

    restored = TenantContext.model_validate_json(context.model_dump_json())

    assert restored == context
