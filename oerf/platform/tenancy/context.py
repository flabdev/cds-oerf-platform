"""Current tenant identity for requests, events, and domain operations."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class TenantContext(BaseModel):
    """Identity of the tenant an operation must run under.

    Cold-chain rules do not belong here. Callers pass this object into
    repositories and domain services so work cannot proceed without a tenant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str

    @field_validator("tenant_id")
    @classmethod
    def tenant_id_must_be_present(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("tenant_id must be a non-empty string")
        return stripped


def get_tenant_context(source: Mapping[str, object]) -> TenantContext:
    """Build a tenant context from a mapping that carries ``tenant_id``.

    Missing, null, and blank ids raise ``ValidationError``.
    """

    if "tenant_id" not in source or source["tenant_id"] is None:
        raise ValidationError.from_exception_data(
            title=TenantContext.__name__,
            line_errors=[
                {
                    "type": "missing",
                    "loc": ("tenant_id",),
                    "input": dict(source),
                }
            ],
        )
    return TenantContext.model_validate({"tenant_id": source["tenant_id"]})
