"""Party address: the registry when mocked, SSM when channels are live."""

import pytest

from oerf.platform.channels.address import AddressError, channels_are_live, resolve_party_address
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


def test_live_channels_read_the_demo_numbers_parameter() -> None:
    parameters = _Parameters({"/oerf/demo-numbers/t1/driver": f"  {LIVE_VALUE}  "})

    found = resolve_party_address(
        "t1",
        "driver",
        "C-T1-DRV",
        registry=None,
        parameters=parameters,
        live=True,
    )

    assert found == LIVE_VALUE
    assert parameters.names == ["/oerf/demo-numbers/t1/driver"]


def test_an_empty_parameter_names_no_number() -> None:
    parameters = _Parameters({"/oerf/demo-numbers/t1/driver": "   "})

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
