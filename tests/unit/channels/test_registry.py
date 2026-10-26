"""Phone registry isolation and shared numbers."""

import pytest

from oerf.platform.channels.registry import DynamoPhoneRegistry, MemoryPhoneRegistry
from tests.unit.support.fake_dynamo import FakeDynamoClient


@pytest.fixture(params=["memory", "dynamo"])
def registry(request):
    if request.param == "memory":
        return MemoryPhoneRegistry()
    return DynamoPhoneRegistry(FakeDynamoClient(), "routing", "tenant")


def test_shared_number_returns_every_tenant_match(registry) -> None:
    registry.register_contact("t1", "C-T1-DRV", "driver", "+910000000101")
    registry.register_contact("t2", "C-T2-DRV", "driver", "+910000000101")

    resolved = registry.resolve_sender("+910000000101")

    assert resolved.unknown is False
    assert {(match.tenant_id, match.contact_id, match.role) for match in resolved.matches} == {
        ("t1", "C-T1-DRV", "driver"),
        ("t2", "C-T2-DRV", "driver"),
    }


@pytest.mark.security
def test_a_tenant_number_does_not_include_another_tenant(registry) -> None:
    registry.register_contact("t1", "C-T1-DRV", "driver", "+910000000101")
    registry.register_contact("t2", "C-T2-OPS", "ops_manager", "+910000000201")

    resolved = registry.resolve_sender("+910000000101")

    assert [match.tenant_id for match in resolved.matches] == ["t1"]
    assert registry.get_contact("t2", "C-T1-DRV") is None


def test_unknown_sender_is_explicit(registry) -> None:
    resolved = registry.resolve_sender("+910000000999")

    assert resolved.unknown is True
    assert resolved.matches == []


def test_reregister_drops_the_previous_number(registry) -> None:
    registry.register_contact("t1", "C-T1-DRV", "driver", "+910000000101")
    registry.register_contact("t2", "C-T2-DRV", "driver", "+910000000101")

    registry.register_contact("t1", "C-T1-DRV", "driver", "+910000000109")

    old = registry.resolve_sender("+910000000101")
    new = registry.resolve_sender("+910000000109")
    assert [match.contact_id for match in old.matches] == ["C-T2-DRV"]
    assert [(match.tenant_id, match.e164) for match in new.matches] == [("t1", "+910000000109")]
    contact = registry.get_contact("t1", "C-T1-DRV")
    assert contact is not None
    assert contact.e164 == "+910000000109"


def test_opt_out_updates_the_contact(registry) -> None:
    registry.register_contact("t1", "C-T1-FACA", "facility", "+910000000141", opt_in=True)

    registry.set_opt_in("t1", "C-T1-FACA", False)

    contact = registry.get_contact("t1", "C-T1-FACA")
    assert contact is not None
    assert contact.opt_in is False
    assert registry.resolve_sender("+910000000141").matches[0].opt_in is False
