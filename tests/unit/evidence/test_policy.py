"""The tenant's own configuration sets the floor and the known fault codes."""

from oerf.platform.evidence import policy


def _readers(
    *,
    codes: dict[str, set[str]] | None = None,
    min_confidence: float | None = None,
    tenant: bool = True,
) -> tuple[object, object]:
    overrides = {"t1": {"evidence": {"min_confidence": min_confidence}}} if min_confidence else {}
    tenants = {"t1": {"tenant_id": "t1"}} if tenant else {}
    return policy.memory_readers(tenants=tenants, codes=codes or {}, configs=overrides)


def test_the_floor_comes_from_the_tenant_configuration() -> None:
    tenant, config = _readers(codes={"t1": {"E17"}}, min_confidence=0.55)

    resolved = policy.resolve_policy("t1", tenant=tenant, config=config)

    assert resolved.min_confidence == 0.55
    assert resolved.source == "tenant_config"
    assert resolved.known_fault_codes == frozenset({"E17"})


def test_without_a_readable_config_the_floor_falls_back() -> None:
    tenant, config = _readers(codes={"t1": {"E17"}}, tenant=False)

    resolved = policy.resolve_policy("t1", tenant=tenant, config=config)

    assert resolved.min_confidence == policy.FALLBACK_MIN_CONFIDENCE
    assert resolved.source == "fallback"
    assert resolved.known_fault_codes == frozenset({"E17"})


def test_an_unreadable_catalog_degrades_to_nothing_verified() -> None:
    class _Broken:
        def tenant_metadata(self, tenant_id: str) -> dict:
            return {}

        def fault_codes(self, tenant_id: str) -> set[str]:
            raise RuntimeError("AccessDeniedException")

    resolved = policy.resolve_policy("t1", tenant=_Broken())

    assert resolved.known_fault_codes == frozenset()
    assert resolved.knows_fault_code("E17") is False


def test_an_unknown_tenant_never_raises() -> None:
    resolved = policy.resolve_policy("nobody")

    assert resolved.tenant_id == "nobody"
    assert resolved.known_fault_codes == frozenset()
    assert resolved.source == "fallback"


def test_the_confirmation_gate_uses_the_tenant_floor() -> None:
    resolved = policy.EvidencePolicy(tenant_id="t1", min_confidence=0.8)

    assert resolved.confirmed(0.8) is True
    assert resolved.confirmed(0.79) is False
    assert resolved.confirmed(None) is False


def test_a_catalog_row_is_read_from_the_sort_key_or_the_code_field() -> None:
    by_key = policy.codes_in({"sk": {"S": "FAULTCODE#e17"}})
    by_field = policy.codes_in({"sk": {"S": "FAULTCODE#E17"}, "code": {"S": "e17"}})

    assert by_key == {"E17"}
    assert by_field == {"E17"}
    assert policy.codes_in({"sk": {"S": "META"}}) == set()


def test_the_tenant_table_reader_queries_only_that_tenant() -> None:
    calls: list[dict] = []

    class _Client:
        def get_item(self, **kwargs):
            calls.append(kwargs)
            return {"Item": {"tenant_id": {"S": "t1"}}}

        def query(self, **kwargs):
            calls.append(kwargs)
            return {"Items": [{"sk": {"S": "FAULTCODE#E17"}}]}

    tenant, _ = policy.dynamo_readers(_Client(), "tenant-table")

    assert tenant.tenant_metadata("t1") == {"tenant_id": {"S": "t1"}}
    assert tenant.fault_codes("t1") == {"E17"}
    assert calls[0]["Key"]["pk"]["S"] == "TENANT#t1"
    assert calls[1]["ExpressionAttributeValues"][":pk"]["S"] == "TENANT#t1"
    assert calls[1]["ExpressionAttributeValues"][":prefix"]["S"] == "FAULTCODE#"