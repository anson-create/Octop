"""The source lookup: exact enough to cite, honest when it finds nothing."""

from __future__ import annotations

from pathlib import Path

from octop.infra.pm.domain.repos import FRAMEWORK_JAVA, FRAMEWORK_VUE3, RepoSpec
from octop.infra.pm.domain.source_index import (
    query_terms,
    search_repo,
    search_requirement,
)

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
    # Phrase windows and two-character terms alternate: the long window carries
    # the meaning, the short one is often the only thing present in the source.
    assert len(terms) >= 2
    assert any(len(term) >= 4 for term in terms)
    assert any(len(term) < 4 for term in terms)


def test_the_subject_of_the_title_is_not_pushed_out_by_sentence_windows() -> None:
    # A ticket whose body is a paragraph used to spend the whole term budget on
    # windows like 也要带上 / 按启用状, and 空港 — the only word the source file
    # actually contains — never reached grep.
    terms = query_terms(
        "空港管理新增启用状态筛选\n空港列表需要支持按启用状态筛选，导出也要带上这个条件。"
    )

    assert "空港" in terms
    # Title terms come before body terms, not after them.
    assert terms.index("空港") < 4


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


# --- the front-end layer: CargoWareX has a Vue mirror but no api mirror ------

AIRPORT_API = '''/**
 * 空港管理前端接口
 */

import { httpClient as http } from '@/utils/http'

const API_BASE = '/mdm/airport'

export const AirportApi = {
  // 空港分页查询
  queryPage: (params: AirportQueryRequest) => {
    return http.post<PageResultVO<AirportVO>>(`${API_BASE}/queryPage`, params)
  },

  // 查询空港与航线关联列表
  queryRouteRelations: (portCode: string) => {
    return http.post<PortRouteRelationVO[]>(`/mdm/airport/route/queryList`, { portCode })
  },
}
'''

ROUTES = '''import type { RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
  {
    path: '/basic-data/airport',
    name: 'Airport',
    component: () => import('@/views/basic-data/airport/index.vue'),
    meta: { title: '空港管理' },
  },
]
'''

AIRPORT_PAGE = '''<template>
  <div class="airport">
    <a-button>{{ t('airport.action.add') }}</a-button>
  </div>
</template>

<script setup lang="ts">
import { AirportApi } from '@/api/basic-data/airport'
import { uploadFile } from '@/api/universal-file'
</script>
'''


def _web_spec(root: Path, key: str = "cwx-web") -> RepoSpec:
    return RepoSpec(
        key=key,
        system="cargowarex",
        layer="web",
        framework=FRAMEWORK_VUE3,
        mirror=str(root),
    )


def test_a_frontend_api_module_yields_endpoint_plus_method(tmp_path: Path) -> None:
    _write(tmp_path, "src/api/basic-data/airport.ts", AIRPORT_API)

    digest = search_repo(_web_spec(tmp_path), "空港")

    assert digest.ok
    assert digest.layer == "web"
    hit = digest.hits[0]
    assert hit.kind == "endpoint"
    # `${API_BASE}/queryPage` is not a path anyone can look up; the resolved one is.
    assert "POST /mdm/airport/queryPage" in hit.excerpt
    assert "queryPage" in hit.excerpt
    # The comment is the human label, and it travels with the path.
    assert "空港分页查询" in hit.excerpt


def test_a_router_file_says_which_view_a_path_mounts(tmp_path: Path) -> None:
    _write(tmp_path, "src/router/routes.ts", ROUTES)

    digest = search_repo(_web_spec(tmp_path), "空港管理")

    assert digest.ok
    hit = digest.hits[0]
    assert hit.kind == "route"
    assert "路由 /basic-data/airport" in hit.excerpt
    assert "views/basic-data/airport/index.vue" in hit.excerpt


def test_a_page_is_reached_through_its_route_and_names_its_api_modules(
    tmp_path: Path,
) -> None:
    # Page labels live in i18n, so the page file itself contains none of the
    # ticket's words and grep never reaches it. The route does.
    _write(tmp_path, "src/router/routes.ts", ROUTES)
    _write(tmp_path, "src/views/basic-data/airport/index.vue", AIRPORT_PAGE)

    digest = search_repo(_web_spec(tmp_path), "空港管理")

    pages = [hit for hit in digest.hits if hit.kind == "page"]
    assert pages, digest.hits
    assert pages[0].path.endswith("views/basic-data/airport/index.vue")
    assert pages[0].excerpt.startswith("airport:")
    assert "@/api/basic-data/airport" in pages[0].excerpt


def test_the_frontend_build_output_is_never_quoted(tmp_path: Path) -> None:
    # `dist/` and `node_modules/` repeat the same vocabulary thousands of times
    # and none of it is source anyone edited.
    _write(tmp_path, "dist/assets/index-abc.js", "空港 空港 空港")
    _write(tmp_path, "node_modules/some-lib/readme.ts", "空港")
    _write(tmp_path, "src/api/basic-data/airport.ts", AIRPORT_API)

    digest = search_repo(_web_spec(tmp_path), "空港")

    assert digest.ok
    assert all("dist/" not in hit.path and "node_modules/" not in hit.path for hit in digest.hits)


def test_the_api_layer_is_read_before_the_front_end(tmp_path: Path) -> None:
    api_root = tmp_path / "cw-api"
    web_root = tmp_path / "cwx-web"
    _write(api_root, "act-service/src/main/java/com/walltech/cargo/controller/AirportController.java", CONTROLLER.replace("ChargeConfirmed", "Airport").replace("/chargeConfirmed", "/airport"))
    _write(web_root, "src/api/basic-data/airport.ts", AIRPORT_API)

    digest = search_requirement(
        [_spec(api_root), _web_spec(web_root)], "供应商费用确认"
    )

    assert digest is not None
    assert digest.layer == "api"
    # Nothing was skipped, because the first layer answered.
    assert digest.skipped == ()


def test_a_missing_api_mirror_falls_back_to_the_front_end_and_says_so(
    tmp_path: Path,
) -> None:
    web_root = tmp_path / "cwx-web"
    _write(web_root, "src/api/basic-data/airport.ts", AIRPORT_API)

    digest = search_requirement(
        [
            RepoSpec(
                key="cwx-api",
                system="cargowarex",
                layer="api",
                framework=FRAMEWORK_JAVA,
                mirror=str(tmp_path / "never-synced"),
            ),
            _web_spec(web_root),
        ],
        "空港",
    )

    assert digest is not None
    assert digest.ok and digest.layer == "web"
    # "read the front end instead" must not read as "read everything".
    assert any("cwx-api" in note and "镜像未同步" in note for note in digest.skipped)
    assert "未检索" in digest.render()


def test_when_every_layer_fails_the_reason_names_them_all(tmp_path: Path) -> None:
    digest = search_requirement(
        [
            RepoSpec(
                key="cwx-api",
                system="cargowarex",
                layer="api",
                framework=FRAMEWORK_JAVA,
                mirror=str(tmp_path / "no-api"),
            ),
            RepoSpec(
                key="cwx-web",
                system="cargowarex",
                layer="web",
                framework=FRAMEWORK_VUE3,
                mirror=str(tmp_path / "no-web"),
            ),
        ],
        "空港",
    )

    assert digest is not None and not digest.ok
    assert "cwx-api" in digest.reason and "cwx-web" in digest.reason


def test_a_page_matching_only_a_generic_word_is_not_evidence(tmp_path: Path) -> None:
    # "表单" appears in half the repository; "询价表单" appears in one place.
    _write(
        tmp_path,
        "src/views/app-management/app-access/index.vue",
        "<template><div><!-- 配置表单 --></div></template>\n",
    )
    _write(
        tmp_path,
        "src/views/inquiry/form/index.vue",
        "<template><div><!-- 询价表单字段配置 --></div></template>\n",
    )

    digest = search_repo(_web_spec(tmp_path), "询价表单字段问题")

    paths = [hit.path for hit in digest.hits]
    assert any("inquiry/form" in path for path in paths), paths
    assert all("app-management" not in path for path in paths), paths


def test_a_router_is_silent_when_no_route_is_about_the_ticket(tmp_path: Path) -> None:
    # The router mentions 供应商 in a comment, but no route mounts such a page.
    # Quoting its first four routes would look like evidence and be noise.
    _write(
        tmp_path,
        "src/router/routes.ts",
        "// 供应商改造尚未接入路由\n"
        "export const routes = [\n"
        "  { path: '/auth/admin', component: () => import('@/views/auth/admin-login.vue') },\n"
        "  { path: '/dashboard', component: () => import('@/views/dashboard/index.vue') },\n"
        "]\n",
    )

    digest = search_repo(_web_spec(tmp_path), "供应商")

    assert not digest.ok
    assert "没有可读的结构" in digest.reason


def test_the_endpoint_quoted_is_the_one_whose_comment_matches(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/api/price.ts",
        "const API_BASE = '/price'\n"
        "export const PriceApi = {\n"
        "  // 分页查询整箱运价\n"
        "  page: (p) => http.post(`${API_BASE}/fcl/page`, p),\n"
        "  // 供应商询价单保存\n"
        "  saveInquiry: (p) => http.post(`${API_BASE}/inquiry/save`, p),\n"
        "}\n",
    )

    digest = search_repo(_web_spec(tmp_path), "供应商询价单保存")

    assert digest.ok
    excerpt = digest.hits[0].excerpt
    assert "/price/inquiry/save" in excerpt
    assert "/price/fcl/page" not in excerpt


# --- the product's own label dictionary -------------------------------------
#
# The failure being tested: a CCTOWER ticket is written in Chinese, the api
# mirror is written in English, and the lookup answered "no existing
# implementation" for pages that had shipped for years.

PORT_ZH = """// 港口管理模块中文翻译
export default {
  // 页面标题
  title: '港口管理',
  pageTitle: '港口管理 - 平台管理后台',
  tabs: {
    seaport: '海港',
    airport: '空港'
  },
  search: {
    status: '状态',
    statusPlaceholder: '选择状态'
  },
  status: {
    enabled: '启用',
    disabled: '禁用'
  },
  actions: {
    addAirport: '新增空港',
    batchEnable: '批量启用'
  }
}
"""

PORT_EN = """export default {
  title: 'Port Management',
  pageTitle: 'Port Management - Platform Admin',
  tabs: {
    seaport: 'Seaports',
    airport: 'Airports'
  },
  search: {
    status: 'Status',
    statusPlaceholder: 'Select status'
  },
  status: {
    enabled: 'Enabled',
    disabled: 'Disabled'
  },
  actions: {
    addAirport: 'Add airport',
    batchEnable: 'Bulk enable'
  }
}
"""

AIRPORT_CONTROLLER = """package com.walltech.control.tower.mdm.controller;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/mdm/airport")
public class AirportController {

    @PostMapping("queryList")
    public Result<PageInfo<AirportVO>> queryAirportList(@RequestBody AirportQuery request) {
        return Result.ok(null);
    }

    @PostMapping("enable")
    public Result<Void> setEnabled(@RequestBody AirportEnableRequest request) {
        return Result.ok(null);
    }
}
"""


def test_a_chinese_title_reaches_the_english_controller_through_the_labels(
    tmp_path: Path,
) -> None:
    api_root = tmp_path / "cwx-api"
    web_root = tmp_path / "cwx-web"
    _write(
        api_root,
        "mdm-service/src/main/java/com/walltech/control/tower/mdm/controller/AirportController.java",
        AIRPORT_CONTROLLER,
    )
    _write(web_root, "src/i18n/modules/zh-CN/port.ts", PORT_ZH)
    _write(web_root, "src/i18n/modules/en-US/port.ts", PORT_EN)

    digest = search_requirement(
        [_spec(api_root, "cwx-api"), _web_spec(web_root)], "空港管理新增启用状态筛选"
    )

    assert digest is not None
    assert digest.ok
    # The api layer answers, not the front end: it was only reached because the
    # label dictionary turned 空港 into a word the Java actually uses.
    assert digest.layer == "api"
    assert "Airport" in digest.terms
    assert "AirportController" in digest.hits[0].path


def test_the_dictionary_is_read_from_the_front_end_not_the_api_mirror(
    tmp_path: Path,
) -> None:
    """A locale file inside the api mirror is not a dictionary."""

    api_root = tmp_path / "cwx-api"
    _write(api_root, "src/i18n/modules/zh-CN/port.ts", PORT_ZH)
    _write(api_root, "src/i18n/modules/en-US/port.ts", PORT_EN)

    digest = search_requirement([_spec(api_root, "cwx-api")], "空港管理新增启用状态筛选")

    assert digest is not None
    assert not digest.ok
    assert "Airport" not in digest.terms


def test_a_label_shared_by_the_whole_product_is_not_a_search_term(tmp_path: Path) -> None:
    """80 labels say 说明; a query for it identifies no feature, so nothing is
    invented to grep for."""

    web_root = tmp_path / "cwx-web"
    lines = "\n".join(f"    k{index}: '说明{index}'" for index in range(80))
    _write(
        web_root,
        "src/i18n/modules/zh-CN/common.ts",
        f"export default {{\n  broadcast: {{\n{lines}\n  }}\n}}\n",
    )
    _write(
        web_root,
        "src/i18n/modules/en-US/common.ts",
        f"export default {{\n  broadcast: {{\n{lines.replace('说明', 'Note ')}\n  }}\n}}\n",
    )

    digest = search_requirement([_web_spec(web_root)], "说明")

    assert digest is not None
    assert digest.terms == query_terms("说明")


def test_a_matched_label_still_reads_when_the_english_twin_is_missing(tmp_path: Path) -> None:
    """Only zh-CN was synced: the key names still carry English identifiers."""

    api_root = tmp_path / "cwx-api"
    web_root = tmp_path / "cwx-web"
    _write(
        api_root,
        "mdm-service/src/main/java/com/walltech/control/tower/mdm/controller/AirportController.java",
        AIRPORT_CONTROLLER,
    )
    _write(web_root, "src/i18n/modules/zh-CN/port.ts", PORT_ZH)

    digest = search_requirement(
        [_spec(api_root, "cwx-api"), _web_spec(web_root)], "空港管理新增启用状态筛选"
    )

    assert digest is not None
    assert digest.ok
    assert "Airport" in digest.terms
