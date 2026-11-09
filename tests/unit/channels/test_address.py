"""Party address: the registry when mocked, SSM when channels are live."""

import pytest

from oerf.platform.channels.address import (
    AddressError,
    channels_are_live,
    resolve_party_address,
)
from oerf.platform.channels.registry import MemoryPhoneRegistry

PLACEHOLDER = "+910000000101"
LIVE_VALUE = "+910000000199"


class _Parameters:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.names: list[str] = []

    def get(self, name: str) -> str:
        self.names.append(name)
        if name not in self.values:
            raise AddressError("the demo-numbers parameter is missing")
        return self.values[name]


def test_mocked_channels_read_the_contact_registry() -> None:
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-T1-DRV", "driver", PLACEHOLDER)
    parameters = _Parameters({"/oerf/demo-numbers/t1/driver": LIVE_VALUE})

    found = resolve_party_address(
        "t1",
        "driver",
        "C-T1-DRV",
        registry=registry,
        parameters=parameters,
        live=False,
    )

    assert found == PLACEHOLDER
    assert parameters.names == []


def test_live_channels_read_the_demo_numbers_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_NUMBERS_PREFIX", "/oerf/dev/demo-numbers")
    parameters = _Parameters({"/oerf/dev/demo-numbers/t1/driver/C-T1-DRV": f"  {LIVE_VALUE}  "})

    found = resolve_party_address(
        "t1",
        "driver",
        "C-T1-DRV",
        registry=None,
        parameters=parameters,
        live=True,
    )

    assert found == LIVE_VALUE
    assert parameters.names == ["/oerf/dev/demo-numbers/t1/driver/C-T1-DRV"]


def test_live_driver_address_is_contact_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_NUMBERS_PREFIX", "/oerf/dev/demo-numbers")
    parameters = _Parameters(
        {
            "/oerf/dev/demo-numbers/t1/driver/C-T1-ANANDA": "+15550100901",
            "/oerf/dev/demo-numbers/t1/driver/C-T1-MUDITA": "+15550100902",
        }
    )

    first = resolve_party_address(
        "t1", "driver", "C-T1-ANANDA", registry=None, parameters=parameters, live=True
    )
    second = resolve_party_address(
        "t1", "driver", "C-T1-MUDITA", registry=None, parameters=parameters, live=True
    )

    assert first == "+15550100901"
    assert second == "+15550100902"
    assert parameters.names == [
        "/oerf/dev/demo-numbers/t1/driver/C-T1-ANANDA",
        "/oerf/dev/demo-numbers/t1/driver/C-T1-MUDITA",
    ]


def test_live_driver_address_uses_stage_configured_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_NUMBERS_PREFIX", "/oerf/demo-numbers")
    parameters = _Parameters({"/oerf/demo-numbers/t1/driver/C-T1-DRV": LIVE_VALUE})

    found = resolve_party_address(
        "t1", "driver", "C-T1-DRV", registry=None, parameters=parameters, live=True
    )

    assert found == LIVE_VALUE
    assert parameters.names == ["/oerf/demo-numbers/t1/driver/C-T1-DRV"]


def test_driver_parameter_name_requires_contact_id() -> None:
    with pytest.raises(AddressError, match="tenant and contact"):
        resolve_party_address(
            "t1", "driver", " ", registry=None, parameters=_Parameters({}), live=True
        )


def test_live_non_driver_address_keeps_role_parameter() -> None:
    parameters = _Parameters({"/oerf/demo-numbers/t1/ops_manager": LIVE_VALUE})

    found = resolve_party_address(
        "t1", "ops_manager", "C-T1-OPS", registry=None, parameters=parameters, live=True
    )

    assert found == LIVE_VALUE
    assert parameters.names == ["/oerf/demo-numbers/t1/ops_manager"]


def test_an_empty_parameter_names_no_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_NUMBERS_PREFIX", "/oerf/dev/demo-numbers")
    parameters = _Parameters({"/oerf/dev/demo-numbers/t1/driver/C-T1-DRV": "   "})

    with pytest.raises(AddressError) as error:
        resolve_party_address(
            "t1",
            "driver",
            "C-T1-DRV",
            registry=None,
            parameters=parameters,
            live=True,
        )

    assert LIVE_VALUE not in str(error.value)
    assert "empty" in str(error.value)


def test_channels_are_live_only_when_the_mock_flag_is_false() -> None:
    assert channels_are_live("false") is True
    assert channels_are_live("true") is False
    assert channels_are_live(" TRUE ") is False
