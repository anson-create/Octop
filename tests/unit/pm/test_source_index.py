"""The source lookup: exact enough to cite, honest when it finds nothing."""

from __future__ import annotations

from pathlib import Path

from octop.infra.pm.domain.repos import FRAMEWORK_JAVA, RepoSpec
from octop.infra.pm.domain.source_index import query_terms, search_repo

CONTROLLER = '''package com.walltech.cargo.controller;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 供应商费用确认
 */
@RestController
@RequestMapping("/chargeConfirmed")
public class ChargeConfirmedController {

    @PostMapping(value = "supplierCharge/advancedSearch")
    public Result<PageInfo<ChargeSyncConfirmOutVO>> supplierChargeAdvancedSearch(
            @RequestBody @Valid QuerySupplierChargeRequest request) {
        return Result.ok(null);
    }
}
'''

SERVICE = '''package com.walltech.cargo.service;

public class SupplierService {
    /** 按就绪状态筛选供应商 */
    public void listSuppliersByReadyState() {
    }
}
'''


def _spec(root: Path, key: str = "cw-api") -> RepoSpec:
    return RepoSpec(
        key=key,
        system="cargoware",
        layer="api",
        framework=FRAMEWORK_JAVA,
        mirror=str(root),
    )


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_long_title_is_cut_into_searchable_windows() -> None:
    # The whole phrase matches nothing in Java source; a window lands on the word.
    terms = query_terms("派单供应商列表新增就绪状态筛选")

    assert "就绪状态" in terms or "供应商列" in terms
    assert all(2 <= len(term) <= 6 for term in terms)
    # Longest first: precision before domain noise.
    lengths = [len(term) for term in terms]
    assert lengths == sorted(lengths, reverse=True)


def test_words_that_identify_nothing_are_dropped() -> None:
    # A title made entirely of filler has nothing to grep for, and saying so is
    # better than grepping "需求" across 10,000 files.
    assert query_terms("需求") == ()
    assert query_terms("") == ()


def test_an_endpoint_is_reported_with_its_mapping_and_method(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "act-service/src/main/java/com/walltech/cargo/controller/ChargeConfirmedController.java",
        CONTROLLER,
    )

    digest = search_repo(_spec(tmp_path), "供应商费用确认")

    assert digest.ok
    hit = digest.hits[0]
    assert hit.kind == "endpoint"
    # The pair is the point: a path alone does not say what it does, and a
    # method path without its class prefix ("POST supplierCharge/...") is a path
    # nobody can call or look up.
    assert "POST /chargeConfirmed/supplierCharge/advancedSearch" in hit.excerpt
    assert "supplierChargeAdvancedSearch" in hit.excerpt
    assert hit.lines == "11-15"
    assert hit.path.endswith("ChargeConfirmedController.java")


def test_the_render_carries_paths_so_a_claim_can_be_checked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "act-service/src/main/java/com/walltech/cargo/controller/ChargeConfirmedController.java",
        CONTROLLER,
    )

    text = search_repo(_spec(tmp_path), "供应商费用确认").render()

    assert "cw-api" in text
    assert "ChargeConfirmedController.java:" in text


def test_a_controller_outranks_a_plain_service(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "act-service/src/main/java/com/walltech/cargo/controller/SupplierController.java",
        CONTROLLER,
    )
    _write(
        tmp_path,
        "act-service/src/main/java/com/walltech/cargo/service/SupplierService.java",
        SERVICE,
    )

    digest = search_repo(_spec(tmp_path), "供应商", limit=2)

    assert [hit.kind for hit in digest.hits][0] == "endpoint"


def test_a_service_without_annotations_still_yields_its_vocabulary(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "act-service/src/main/java/com/walltech/cargo/service/SupplierService.java",
        SERVICE,
    )

    digest = search_repo(_spec(tmp_path), "就绪状态")

    assert digest.ok
    assert "就绪状态" in digest.hits[0].excerpt


def test_a_missing_mirror_says_so_instead_of_returning_nothing(tmp_path: Path) -> None:
    digest = search_repo(_spec(tmp_path / "absent"), "供应商")

    # "no hits" and "cannot look" are different facts; the caller writes them
    # into the gap list differently.
    assert not digest.ok
    assert "镜像未同步" in digest.reason
    assert str(tmp_path / "absent") in digest.reason


def test_no_match_is_a_reason_not_an_empty_success(tmp_path: Path) -> None:
    _write(tmp_path, "a/src/main/java/com/walltech/cargo/controller/OtherController.java", CONTROLLER)

    digest = search_repo(_spec(tmp_path), "冷链温控")

    assert not digest.ok
    assert "没有命中" in digest.reason
    assert digest.terms


def test_build_output_and_tests_are_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "act-service/target/classes/SupplierController.java",
        CONTROLLER,
    )
    _write(
        tmp_path,
        "act-service/src/test/java/com/walltech/cargo/controller/SupplierControllerTest.java",
        CONTROLLER,
    )

    digest = search_repo(_spec(tmp_path), "供应商")

    assert not digest.ok


def test_a_two_character_business_term_rescues_a_title_that_never_appears_whole(
    tmp_path: Path,
) -> None:
    # "亿通账单需显示工作单号" is not a phrase any file contains; 亿通 is.
    _write(
        tmp_path,
        "finance-service/src/main/java/com/walltech/cargo/service/EytoneBillService.java",
        'package com.walltech.cargo.service;\n\n'
        "public class EytoneBillService {\n"
        "    /** 亿通 账单导出补充工作单号 */\n"
        "    public void fillWorkNo() {\n    }\n}\n",
    )

    digest = search_repo(_spec(tmp_path), "亿通账单需显示工作单号")

    assert digest.ok
    assert "亿通" in digest.hits[0].excerpt


def test_a_constant_table_does_not_outrank_the_code_that_handles_it(tmp_path: Path) -> None:
    # The phrase appears on every line of the constant table and once in the
    # service. Counting occurrences ranks them backwards; counting vocabulary
    # over the file's purpose does not.
    constants = "\n".join(f'    String X{i} = "海出-海运订舱-代收垫付";' for i in range(30))
    _write(
        tmp_path,
        "commons/common-service/src/main/java/com/walltech/common/constant/AdvanceConstant.java",
        f"package com.walltech.common.constant;\n\npublic class AdvanceConstant {{\n{constants}\n}}\n",
    )
    _write(
        tmp_path,
        "finance-service/src/main/java/com/walltech/cargo/service/AdvanceService.java",
        "package com.walltech.cargo.service;\n\n"
        "public class AdvanceService {\n"
        "    /** 代收垫付 外币与人民币关联 */\n"
        "    public void linkCurrency() {\n    }\n}\n",
    )

    digest = search_repo(_spec(tmp_path), "代收垫付外币与人民币关联", limit=2)

    assert digest.hits[0].path.endswith("AdvanceService.java")
