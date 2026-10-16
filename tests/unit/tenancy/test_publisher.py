"""Draft validation, immutable publish, and the T2 allowance change."""

import json
from datetime import UTC, datetime

import pytest

from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.rules.options import AssessmentContext, RecoveryOption, rank_options
from oerf.platform.storage.dynamo import key, n, s
from oerf.platform.tenancy.config_resolver import ConfigInvalid, ResolvedConfig, get_config
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.publisher import ConfigPublisher
from tests.unit.support.fake_dynamo import FakeDynamoClient


def test_invalid_draft_cannot_publish() -> None:
    publisher, _repository = _publisher()
    publisher.save_draft("t2", "cold_chain_excursion", _profiles(45, range_c=[8, 2]))

    issues = publisher.validate_draft("t2", "cold_chain_excursion")

    assert issues
    assert issues[0].path
    assert issues[0].message
    with pytest.raises(ConfigInvalid):
        publisher.publish("t2", "cold_chain_excursion", published_by="tenant_admin")
    assert _repository.read_version("t2", "cold_chain_excursion", 1) is None


def test_seeded_v1_without_active_publishes_version_2() -> None:
    publisher, repository = _publisher()
    repository._client.put_item(
        TableName="tenant",
        Item={
            **key("TENANT#t2", "CONFIG#cold_chain_excursion#v1"),
            "tenant_id": s("t2"),
            "version": n(1),
            "product_profiles": s(json.dumps(_profiles(45)["product_profiles"])),
        },
    )
    publisher.save_draft("t2", "cold_chain_excursion", _profiles(30))

    published = publisher.publish("t2", "cold_chain_excursion", published_by="tenant_admin")
    current = get_config("t2", "cold_chain_excursion", repository=repository)
    seeded = repository.read_version("t2", "cold_chain_excursion", 1)

    assert published.config_version == 2
    assert published.previous_version == 1
    assert current.version == 2
    assert current.product_profile("PP-2-8-45").excursion_allowance_min == 30
    assert seeded is not None
    assert seeded["product_profiles"][0]["excursion_allowance_min"] == 45


def test_publish_is_immutable_and_pins_open_incidents() -> None:
    publisher, repository = _publisher()
    publisher.save_draft("t2", "cold_chain_excursion", _profiles(45))

    first = publisher.publish("t2", "cold_chain_excursion", published_by="tenant_admin")
    opened = get_config("t2", "cold_chain_excursion", repository=repository)
    incident = IncidentRepository(clock=_clock).create(
        tenant_id="t2",
        shipment_id="PH-1",
        reason="TEMPERATURE_ABOVE_MAX",
        config_version=opened.version,
        config_snapshot=opened.model_dump(),
    )
    assert first.config_version == 1
    assert first.previous_version is None
    assert first.published_by == "tenant_admin"
    assert _facility_b_feasible(opened) is True

    publisher.save_draft("t2", "cold_chain_excursion", _profiles(30))
    second = publisher.publish("t2", "cold_chain_excursion", published_by="tenant_admin")
    revised = get_config("t2", "cold_chain_excursion", repository=repository)

    assert second.config_version == 2
    assert second.previous_version == 1
    assert second.status == "published"
    assert _facility_b_feasible(revised) is False
    assert repository.read_version("t2", "cold_chain_excursion", 1)["version"] == 1
    assert incident.config_version == 1
    assert incident.config_snapshot is not None
    assert incident.config_snapshot["version"] == 1
    assert ResolvedConfig.model_validate(incident.config_snapshot).product_profile(
        "PP-2-8-45"
    ).excursion_allowance_min == 45


def _publisher() -> tuple[ConfigPublisher, DynamoConfigRepository]:
    repository = DynamoConfigRepository(FakeDynamoClient(), "tenant")
    repository.register_tenant("t2", ["cold_chain_excursion"])
    publisher = ConfigPublisher(repository, clock=_clock)
    return publisher, repository


def _profiles(allowance: float, *, range_c: list[float] | None = None) -> dict[str, object]:
    return {
        "product_profiles": [
            {
                "id": "PP-2-8-45",
                "name": "Biologic 2-8",
                "range_c": range_c or [2, 8],
                "excursion_allowance_min": allowance,
                "allowance_mode": "cumulative",
            }
        ]
    }


def _facility_b_feasible(config: ResolvedConfig) -> bool:
    profile = config.product_profile("PP-2-8-45")
    result = rank_options(
        AssessmentContext(
            upper_c=profile.range_c[1],
            allowance_minutes=profile.excursion_allowance_min,
            used_minutes=3,
            temperature_c=8.19,
            unload_min=config.assessment.unload_min,
            rise_rate_c_per_min=config.assessment.rise_rate_c_per_min,
            cooldown_rate_c_per_min=config.assessment.cooldown_rate_c_per_min,
            min_spare_allowance_min=config.assessment.min_spare_allowance_min,
        ),
        [RecoveryOption(id="FACILITY_B", drive_min=21)],
    )
    return result.options[0].feasible


def _clock() -> datetime:
    return datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
