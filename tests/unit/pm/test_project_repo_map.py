"""A requirement's source is decided by its Jira project code, not by search.

The rule these tests defend: the mapping is explicit and inspectable, and an
unknown project resolves to nothing. Reading the wrong product's source is worse
than reading none — the resulting prototype is structurally faithful and entirely
wrong, which is exactly the failure that is hardest to notice.
"""

from __future__ import annotations

import pytest

from octop.infra.pm import errors
from octop.infra.pm.domain.repos import (
    REPOS,
    project_key_of,
    project_of_requirement,
    resolve_project,
    web_repo_for_project,
)

# --- domain ----------------------------------------------------------------


def test_issue_key_reduces_to_its_project() -> None:
    assert project_key_of("cargo-1234") == "CARGO"
    assert project_key_of("CCTOWER-3597") == "CCTOWER"
    assert project_key_of("") == ""

# --- renderer selection -----------------------------------------------------


def test_each_vue_generation_gets_its_own_renderer() -> None:
    """Vue 2 has no createSSRApp and no @vue/server-renderer; the scripts differ."""

    assert REPOS["cwx-web"].ssr.script == "render.mjs"
    assert REPOS["cw-web"].ssr.script == "render.vue2.mjs"


def test_an_api_layer_has_no_renderer_at_all() -> None:
    """Empty means "cannot render", which is not the same as "use the default"."""

    assert REPOS["cw-api"].ssr.script == ""
    assert REPOS["cw-api"].renderable is False


def test_the_vue2_toolchain_is_kept_out_of_the_product_tree() -> None:
    """CargoWare never depends on vite; putting it there would be wiped by sync."""

    plan = REPOS["cw-web"].ssr

    assert plan.toolchain == "vue2"
    assert "vue-server-renderer" in plan.toolchain_requirements
    assert "vite" not in plan.mirror_requirements


def test_a_vite_app_renders_from_its_own_node_modules() -> None:
    """CargoWareX needs no toolchain: vite is already one of its devDependencies."""

    plan = REPOS["cwx-web"].ssr

    assert plan.toolchain == ""
    assert "vite" in plan.mirror_requirements


def test_non_jira_source_ref_is_not_parsed_as_a_ticket() -> None:
    """`acceptance/2026-10-31` is a folder, not an issue key.

    Parsing it would yield the project "ACCEPTANCE/2026" and the operator would
    be told to map a project that does not exist.
    """

    assert project_of_requirement(None, "acceptance/2026-10-31", source="manual") == ""
    assert project_of_requirement(None, "CARGO-1234", source="jira") == "CARGO"
    # A recorded project wins regardless of source.
    assert project_of_requirement({"jira_project": "CCTOWER"}, "", source="manual") == "CCTOWER"


def test_known_project_binds_to_both_layers() -> None:
    binding = resolve_project("CARGO")

    assert binding is not None
    assert binding.system == "cargoware"
    assert set(binding.repos) == {"cw-web", "cw-api"}
    assert binding.origin == "default"
    assert binding.web is not None and binding.web.ui_lib == "element-ui"


def test_cargowarex_project_binds_to_arco_stack() -> None:
    spec = web_repo_for_project("CCTOWER")

    assert spec is not None
    assert spec.key == "cwx-web"
    assert spec.framework == "vue3"
    assert spec.ui_lib == "arco"


def test_unknown_project_resolves_to_nothing() -> None:
    """No keyword fallback: an unmapped project must ask, never guess."""

    assert resolve_project("OPS") is None
    assert web_repo_for_project("OPS") is None


def test_saved_mapping_overrides_the_seed_table() -> None:
    binding = resolve_project("CARGO", {"CARGO": "cwx-web"})

    assert binding is not None
    assert binding.system == "cargowarex"
    assert binding.repos == ("cwx-web",)
    assert binding.origin == "mapping"


def test_saved_mapping_can_route_a_project_the_seed_table_never_knew() -> None:
    binding = resolve_project("OPS", {"OPS": "cargowarex"})

    assert binding is not None
    assert binding.repos == ("cwx-web", "cwx-api")


def test_mapping_straddling_both_products_is_rejected() -> None:
    """``cw-web,cwx-api`` is a typo, not a deployment; refuse it outright."""

    assert resolve_project("OPS", {"OPS": "cw-web,cwx-api"}) is None


def test_unparseable_mapping_falls_back_rather_than_inventing() -> None:
    assert resolve_project("OPS", {"OPS": "no-such-repo"}) is None
    assert resolve_project("CARGO", {"CARGO": ""}) is not None  # seed table still applies


# --- service ---------------------------------------------------------------


def test_project_list_reports_which_projects_have_no_source(
    service, workspace, fake_jira
) -> None:
    payload = service.jira_projects(workspace["id"])

    by_key = {item["key"]: item for item in payload["items"]}
    assert by_key["CARGO"]["mapped"] is True
    assert by_key["CARGO"]["web_repo"] == "cw-web"
    assert by_key["CCTOWER"]["web_repo"] == "cwx-web"
    # OPS is a real Jira project with no repository behind it.
    assert by_key["OPS"]["mapped"] is False
    assert by_key["OPS"]["web_repo"] == ""
    assert payload["unmapped"] == ["OPS"]
    assert "cwx-web" in payload["repo_keys"]


def test_mapping_is_persisted_and_takes_effect_immediately(
    service, workspace, fake_jira
) -> None:
    service.save_project_mapping(workspace["id"], project="ops", target="cargoware")

    payload = service.jira_projects(workspace["id"])
    by_key = {item["key"]: item for item in payload["items"]}
    assert by_key["OPS"]["mapped"] is True
    assert by_key["OPS"]["system"] == "cargoware"
    assert by_key["OPS"]["origin"] == "mapping"
    assert payload["unmapped"] == []


def test_mapping_accepts_only_real_targets(service, workspace, fake_jira) -> None:
    with pytest.raises(errors.OctopError):
        service.save_project_mapping(workspace["id"], project="OPS", target="somewhere")


def test_empty_target_clears_the_override(service, workspace, fake_jira) -> None:
    service.save_project_mapping(workspace["id"], project="OPS", target="cargoware")
    service.save_project_mapping(workspace["id"], project="OPS", target="")

    payload = service.jira_projects(workspace["id"])
    by_key = {item["key"]: item for item in payload["items"]}
    assert by_key["OPS"]["mapped"] is False


def test_intake_records_the_project_as_a_baseline_fact(
    service, workspace, fake_jira
) -> None:
    requirement = service.create_requirement_from_jira(
        workspace["id"], issue_key="CARGO-1234"
    )

    baseline = requirement["baseline"]
    if isinstance(baseline, str):
        import json

        baseline = json.loads(baseline)
    assert baseline["jira_project"] == "CARGO"
    # The key still stays out of the rendered text.
    assert "CARGO-1234" not in requirement["raw_text"]


def test_requirement_resolves_to_a_concrete_web_repo(
    service, workspace, fake_jira
) -> None:
    requirement = service.create_requirement_from_jira(
        workspace["id"], issue_key="CARGO-1234"
    )

    resolved = service.requirement_source_repo(requirement["id"])
    assert resolved["mapped"] is True
    assert resolved["project"] == "CARGO"
    assert resolved["web_repo"] == "cw-web"
    assert resolved["mirror"].endswith("/cw-web")


def test_requirement_with_unmapped_project_says_why(service, workspace, fake_jira) -> None:
    """The failure has to name the project, or nobody can fix the mapping."""

    requirement = service.create_requirement(
        workspace_id=workspace["id"],
        title="运维支持",
        raw_text="运维侧提出的内部工具需求。",
        source="jira",
        source_ref="OPS-12",
        baseline={"jira_project": "OPS"},
    )

    resolved = service.requirement_source_repo(requirement["id"])
    assert resolved["mapped"] is False
    assert resolved["project"] == "OPS"
    assert "未映射" in resolved["reason"]


def test_requirement_without_a_jira_project_says_so(service, workspace) -> None:
    requirement = service.create_requirement(
        workspace_id=workspace["id"],
        title="无来源",
        raw_text="没有关联工单的需求。",
        source="manual",
    )

    resolved = service.requirement_source_repo(requirement["id"])
    assert resolved["mapped"] is False
    assert resolved["project"] == ""
    assert "Jira" in resolved["reason"]
