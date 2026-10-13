"""Tests for tenant and domain configuration resolution."""

import json
import shutil
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from oerf.platform.tenancy.config_resolver import (
    ConfigInvalid,
    DomainNotFound,
    FileConfigRepository,
    TenantNotFound,
    find_repo_root,
    get_config,
)


def test_t1_cold_chain_config_resolves_product_profiles() -> None:
    config = get_config(tenant_id="t1", domain="cold_chain_excursion")

    biologic = config.product_profile("PP-2-8-45")
    ambient = config.product_profile("PP-15-25-120")

    assert config.tenant_id == "t1"
    assert config.domain == "cold_chain_excursion"
    assert biologic.range_c == [2, 8]
    assert biologic.excursion_allowance_min == 45
    assert ambient.range_c == [15, 25]
    assert ambient.excursion_allowance_min == 120
    assert config.assessment.unload_min == 4
    assert config.trigger.condition == "above_upper"
    assert config.trigger.debounce_readings == 2


def test_unknown_tenant_is_rejected() -> None:
    with pytest.raises(TenantNotFound, match="does not exist"):
        get_config(tenant_id="missing", domain="cold_chain_excursion")


def test_unknown_domain_is_rejected() -> None:
    with pytest.raises(DomainNotFound, match="not subscribed"):
        get_config(tenant_id="t1", domain="no_such_domain")


def test_blank_domain_is_rejected() -> None:
    with pytest.raises(DomainNotFound, match="non-empty"):
        get_config(tenant_id="t1", domain="  ")


def test_blank_tenant_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        get_config(tenant_id="  ", domain="cold_chain_excursion")


def test_tenant_override_replaces_domain_default(tmp_path: Path) -> None:
    root = _copy_config_tree(tmp_path)
    override = root / "sim" / "seed" / "t1" / "domains" / "cold_chain_excursion.json"
    document = json.loads(override.read_text(encoding="utf-8"))
    document["product_profiles"][0]["excursion_allowance_min"] = 30
    override.write_text(json.dumps(document), encoding="utf-8")

    config = get_config(
        tenant_id="t1",
        domain="cold_chain_excursion",
        repository=FileConfigRepository(root),
    )

    assert config.product_profile("PP-2-8-45").excursion_allowance_min == 30
    assert config.assessment.unload_min == 4


def test_invalid_configuration_is_rejected(tmp_path: Path) -> None:
    root = _copy_config_tree(tmp_path)
    override = root / "sim" / "seed" / "t1" / "domains" / "cold_chain_excursion.json"
    document = json.loads(override.read_text(encoding="utf-8"))
    document["product_profiles"][0]["excursion_allowance_min"] = -1
    override.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ConfigInvalid, match="invalid"):
        get_config(
            tenant_id="t1",
            domain="cold_chain_excursion",
            repository=FileConfigRepository(root),
        )


def test_subscribed_domain_without_template_is_rejected(tmp_path: Path) -> None:
    root = _copy_config_tree(tmp_path)
    tenant_path = root / "sim" / "seed" / "t1" / "tenant.json"
    tenant = json.loads(tenant_path.read_text(encoding="utf-8"))
    tenant["domains"].append("missing_domain")
    tenant_path.write_text(json.dumps(tenant), encoding="utf-8")

    with pytest.raises(DomainNotFound, match="does not exist"):
        get_config(
            tenant_id="t1",
            domain="missing_domain",
            repository=FileConfigRepository(root),
        )


def test_domain_and_seed_files_are_package_resources() -> None:
    defaults = files("domains").joinpath("cold_chain_excursion", "defaults.json")
    schema = files("domains").joinpath("cold_chain_excursion", "schema.json")
    tenant = files("sim").joinpath("seed", "t1", "tenant.json")

    assert defaults.is_file()
    assert schema.is_file()
    assert tenant.is_file()
    repository = FileConfigRepository()
    assert repository.read_defaults("cold_chain_excursion")["domain"] == "cold_chain_excursion"


def _copy_config_tree(tmp_path: Path) -> Path:
    source = find_repo_root()
    shutil.copytree(source / "domains", tmp_path / "domains")
    shutil.copytree(source / "sim" / "seed", tmp_path / "sim" / "seed")
    return tmp_path
