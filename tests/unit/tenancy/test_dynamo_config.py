"""DynamoDB config resolution, including a readable T1 v1 row."""

import json

from oerf.platform.storage.dynamo import key, n, s
from oerf.platform.tenancy.config_resolver import get_config
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from tests.unit.support.fake_dynamo import FakeDynamoClient


def test_seed_v1_row_resolves_without_an_active_pointer() -> None:
    client = FakeDynamoClient()
    repository = DynamoConfigRepository(client, "tenant")
    repository.register_tenant("t1", ["cold_chain_excursion"])
    client.put_item(
        TableName="tenant",
        Item={
            **key("TENANT#t1", "CONFIG#cold_chain_excursion#v1"),
            "tenant_id": s("t1"),
            "version": n(1),
            "product_profiles": s(
                json.dumps(
                    [
                        {
                            "id": "PP-2-8-45",
                            "name": "Biologic 2-8",
                            "range_c": [2, 8],
                            "excursion_allowance_min": 45,
                            "allowance_mode": "cumulative",
                        },
                        {
                            "id": "PP-15-25-120",
                            "name": "Ambient 15-25",
                            "range_c": [15, 25],
                            "excursion_allowance_min": 120,
                            "allowance_mode": "cumulative",
                        },
                    ]
                )
            ),
        },
    )

    config = get_config("t1", "cold_chain_excursion", repository=repository)

    assert config.version == 1
    assert config.product_profile("PP-2-8-45").excursion_allowance_min == 45
    assert config.product_profile("PP-15-25-120").excursion_allowance_min == 120
    assert config.assessment.unload_min == 4


def test_resolution_uses_domain_defaults_then_tenant_override() -> None:
    client = FakeDynamoClient()
    repository = DynamoConfigRepository(client, "tenant")
    repository.register_tenant("t1", ["cold_chain_excursion"])
    client.put_item(
        TableName="tenant",
        Item={
            **key("TENANT#t1", "CONFIG#cold_chain_excursion#v1"),
            "product_profiles": s(
                json.dumps(
                    [
                        {
                            "id": "PP-2-8-45",
                            "name": "Biologic 2-8",
                            "range_c": [2, 8],
                            "excursion_allowance_min": 30,
                            "allowance_mode": "cumulative",
                        }
                    ]
                )
            ),
        },
    )

    config = get_config("t1", "cold_chain_excursion", repository=repository)

    assert config.product_profile("PP-2-8-45").excursion_allowance_min == 30
    assert config.assessment.unload_min == 4
