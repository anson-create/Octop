"""代码链路图谱的回归用例。

这里的每个用例都对应一次真实代码打脸：路径少了类级前缀、注释里的花括号
把类边界算错、JdbcTemplate 的内联 SQL 整个漏掉。构造的 Java 片段刻意长得
和仓库里的写法一样（Lombok final 注入、@RequestBody 泛型参数）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from octop.infra.pm.domain.code_graph import (
    inline_sql_tables,
    parse_java,
    parse_mapper,
    store,
    trace,
    walk_roots,
)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


BILLING_CONTROLLER = """package com.demo.billing.controller;

import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;

@RestController
@RequestMapping("/billing")
@RequiredArgsConstructor
public class BillingController {

    private final BillPaymentService billPaymentService;

    /**
     * 付款列表。文档里的 {@code {id}} 不是代码。
     */
    @PostMapping("list")
    public Result<PageInfo<BillPaymentVO>> list(@RequestBody BillQuery query) {
        return billPaymentService.queryPage(query);
    }
}
"""

BILLING_SERVICE = """package com.demo.billing.service;

public interface BillPaymentService {

    PageInfo<BillPaymentVO> queryPage(BillQuery query);
}
"""

BILLING_IMPL = """package com.demo.billing.service.impl;

import com.demo.billing.mapper.BillPaymentMapper;

@Service
public class BillPaymentServiceImpl implements BillPaymentService {

    private final BillPaymentMapper billPaymentMapper;

    @Override
    public PageInfo<BillPaymentVO> queryPage(BillQuery query) {
        int total = billPaymentMapper.countByQuery(query);
        return PageInfo.of(billPaymentMapper.selectPage(query), total);
    }
}
"""

BILLING_MAPPER = """package com.demo.billing.mapper;

@Mapper
public interface BillPaymentMapper {

    int countByQuery(BillQuery query);

    List<BillPaymentVO> selectPage(BillQuery query);
}
"""

BILLING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.demo.billing.mapper.BillPaymentMapper">

  <select id="countByQuery" resultType="int">
    SELECT COUNT(1) FROM t_bill_payment WHERE status != 9
  </select>

  <select id="selectPage" resultType="BillPaymentVO">
    SELECT p.* FROM t_bill_payment p
    LEFT JOIN dim_tenant t ON t.id = p.tenant_id
    WHERE p.status != 9
  </select>
</mapper>
"""


def _billing_repo(tmp_path: Path) -> Path:
    root = tmp_path / "cwx-api"
    base = "billing-service/src/main/java/com/demo/billing"
    _write(root, f"{base}/controller/BillingController.java", BILLING_CONTROLLER)
    _write(root, f"{base}/service/BillPaymentService.java", BILLING_SERVICE)
    _write(root, f"{base}/service/impl/BillPaymentServiceImpl.java", BILLING_IMPL)
    _write(root, f"{base}/mapper/BillPaymentMapper.java", BILLING_MAPPER)
    _write(
        root,
        "billing-service/src/main/resources/mapper/BillPaymentMapper.xml",
        BILLING_XML,
    )
    return root


def test_a_class_level_prefix_is_part_of_the_path() -> None:
    """`POST list` 是废路径：真实地址是 /billing/list。"""

    cls = parse_java(BILLING_CONTROLLER, "BillingController.java")

    assert cls is not None
    assert cls.controller
    assert cls.base_path == "/billing"
    mapped = [method for method in cls.methods if method.http]
    assert [(method.http, method.path) for method in mapped] == [("POST", "/billing/list")]


def test_a_lombok_final_field_is_how_the_controller_reaches_the_service() -> None:
    """@RequiredArgsConstructor 没有 @Autowired，字段照样是注入点。"""

    cls = parse_java(BILLING_CONTROLLER, "BillingController.java")

    assert cls is not None
    assert cls.fields == {"billPaymentService": "BillPaymentService"}
    assert cls.methods[0].calls == ["BillPaymentService#queryPage"]


def test_braces_inside_comments_and_strings_do_not_move_the_class_boundary() -> None:
    source = """package com.demo;

public class Trap {

    /* 这个方法用来 } 骗人 */
    private String tpl = "{not code}";

    // 还有 { 这里
    public void real() {
        String inner = "}";
    }

    public void second() {
    }
}
"""

    cls = parse_java(source, "Trap.java")

    assert cls is not None
    assert [method.name for method in cls.methods] == ["real", "second"]


def test_jdbc_template_sql_is_read_even_though_it_never_touches_a_mapper() -> None:
    segment = (
        'jdbcTemplate.query("SELECT 1 FROM bkp_routing_registry '
        'WHERE table_schema=? AND table_name=?", ps)'
    )

    assert inline_sql_tables(segment) == [("select", ["bkp_routing_registry"])]


def test_a_log_line_that_happens_to_say_from_is_not_a_table() -> None:
    """`loaded from cache` 曾经被当成 from + cache。"""

    assert inline_sql_tables('log.info("loaded from cache, size={}", n)') == []
    assert inline_sql_tables('log.debug("update status from {} to {}", a, b)') == []


def test_a_concatenated_table_name_is_declared_missing_not_guessed() -> None:
    """静态解析的边界：`"SELECT * FROM " + table` 抽不到，就不该编一个。"""

    assert inline_sql_tables('"SELECT * FROM " + table') == []


def test_a_mapper_statement_carries_the_tables_it_reads() -> None:
    namespace, statements = parse_mapper(BILLING_XML, "BillPaymentMapper.xml")

    assert namespace == "com.demo.billing.mapper.BillPaymentMapper"
    assert [(stmt_id, kind) for stmt_id, kind, _ in statements] == [
        ("countByQuery", "select"),
        ("selectPage", "select"),
    ]
    assert statements[1][2] == ["t_bill_payment", "dim_tenant"]


def test_the_whole_chain_answers_which_tables_an_endpoint_touches(tmp_path: Path) -> None:
    """接口 -> Controller -> Service 接口 -> 实现类 -> Mapper -> XML -> 表。"""

    root = _billing_repo(tmp_path)
    out = tmp_path / "graph.sqlite"
    store(walk_roots([str(root)]), str(out))

    conn = sqlite3.connect(out)
    result = trace(conn, "POST /billing/list")

    assert result["found"] is True
    assert result["tables"] == ["dim_tenant", "t_bill_payment"]
    assert set(result["statements"]) == {
        "BillPaymentMapper.countByQuery",
        "BillPaymentMapper.selectPage",
    }


def test_two_classes_with_the_same_simple_name_both_survive(tmp_path: Path) -> None:
    """用简单名做键时会互相覆盖，10918 个文件只建出 8766 个类。"""

    root = tmp_path / "api"
    left = "a/src/main/java/com/demo/left"
    right = "a/src/main/java/com/demo/right"
    _write(root, f"{left}/OrderService.java", "package com.demo.left;\nclass OrderService {}\n")
    _write(root, f"{right}/OrderService.java", "package com.demo.right;\nclass OrderService {}\n")

    graph = walk_roots([str(root)])

    assert len(graph.classes) == 2


FEIGN_CLIENT = """package com.demo.billing.client;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.*;

@FeignClient(name = "control-tower-service", path = "/inner/billing")
public interface BillingFeignClient {

    @PostMapping("list")
    Result<PageInfo<BillPaymentVO>> list(@RequestBody BillQuery query);
}
"""


def test_a_feign_client_is_how_we_call_out_not_an_endpoint_we_serve(tmp_path: Path) -> None:
    """@FeignClient 长得和 Controller 一样，但没有方法体，顺着它走不到 Mapper。

    把它当端点会让分母凭空多出四分之一（本机 5236 条里有 1210 条），而这些
    端点注定追不到表，覆盖率因此被平白压低。
    """

    root = _billing_repo(tmp_path)
    _write(
        root,
        "billing-service/src/main/java/com/demo/billing/client/BillingFeignClient.java",
        FEIGN_CLIENT,
    )
    out = tmp_path / "graph.sqlite"
    store(walk_roots([str(root)]), str(out))

    conn = sqlite3.connect(out)
    owners = {row[0] for row in conn.execute("SELECT cls FROM endpoint")}
    assert "com.demo.billing.client.BillingFeignClient" not in owners
    assert "com.demo.billing.controller.BillingController" in owners


def test_an_interface_is_excluded_by_its_annotation_not_by_its_name() -> None:
    """只认 @FeignClient。按名字猜会踢掉真 Controller，而且数字上看不出来。"""

    named_like_a_client = """package com.demo.billing.api;

import org.springframework.web.bind.annotation.*;

public interface BillingFeignClient {

    @PostMapping("list")
    Result<?> list();
}
"""

    feign = parse_java(FEIGN_CLIENT, "BillingFeignClient.java")
    plain = parse_java(named_like_a_client, "BillingFeignClient.java")

    assert feign is not None and feign.feign is True
    assert plain is not None and plain.feign is False


# ---------------------------------------------------------------------------
# MyBatis-Plus：表名在实体注解上，SQL 由框架生成，仓库里没有一行可读
# ---------------------------------------------------------------------------

ENTITY = """package com.demo.order.entity;

import com.baomidou.mybatisplus.annotation.TableName;

@TableName("t_order")
public class Order {

    private Long id;
    private String orderNo;
}
"""

ORDER_MAPPER = """package com.demo.order.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;

public interface OrderMapper extends BaseMapper<Order> {
}
"""

ORDER_ISERVICE = """package com.demo.order.service;

import com.baomidou.mybatisplus.extension.service.IService;

public interface IOrderService extends IService<Order> {
}
"""

ORDER_SERVICE_IMPL = """package com.demo.order.service.impl;

import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;

@Service
public class OrderServiceImpl extends ServiceImpl<OrderMapper, Order> implements IOrderService {

    public List<Order> listRecent(int hours) {
        return baseMapper.selectList(null);
    }
}
"""

ORDER_CONTROLLER = """package com.demo.order.controller;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/order")
public class OrderController {

    private final IOrderService orderService;

    @GetMapping("detail")
    public Result<Order> detail(@RequestParam Long id) {
        return Result.ok(orderService.getOne(id));
    }
}
"""


def _mybatis_plus_repo(tmp_path: Path) -> Path:
    root = tmp_path / "mp-api"
    base = "order-service/src/main/java/com/demo/order"
    _write(root, f"{base}/entity/Order.java", ENTITY)
    _write(root, f"{base}/mapper/OrderMapper.java", ORDER_MAPPER)
    _write(root, f"{base}/service/IOrderService.java", ORDER_ISERVICE)
    _write(root, f"{base}/service/impl/OrderServiceImpl.java", ORDER_SERVICE_IMPL)
    _write(root, f"{base}/controller/OrderController.java", ORDER_CONTROLLER)
    return root


def test_a_table_name_written_only_on_the_entity_is_still_found(tmp_path: Path) -> None:
    """MyBatis-Plus 的链路里没有 SQL：表名只在 @TableName，绑定只在泛型。"""

    root = _mybatis_plus_repo(tmp_path)
    out = tmp_path / "graph.sqlite"
    store(walk_roots([str(root)]), str(out))

    conn = sqlite3.connect(out)
    result = trace(conn, "GET /order/detail")

    assert result["found"] is True
    assert result["tables"] == ["t_order"]
    # 这条链路一张 SQL 都没有，必须写清楚表是「泛型绑定」推出来的
    assert result["statements"] == []
    assert "IOrderService(service-interface)" in result["owners"]


def test_the_base_mapper_a_service_impl_never_declares_is_followed(tmp_path: Path) -> None:
    """ServiceImpl 子类不声明 baseMapper（在父类里），字段注入抓不到。"""

    root = _mybatis_plus_repo(tmp_path)
    controller = """package com.demo.order.controller;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/order")
public class RecentController {

    private final OrderServiceImpl orderService;

    @GetMapping("recent")
    public List<Order> recent() {
        return orderService.listRecent(24);
    }
}
"""
    _write(
        root,
        "order-service/src/main/java/com/demo/order/controller/RecentController.java",
        controller,
    )
    out = tmp_path / "graph.sqlite"
    store(walk_roots([str(root)]), str(out))

    conn = sqlite3.connect(out)
    result = trace(conn, "GET /order/recent")

    assert result["tables"] == ["t_order"]
    # 依据是 Mapper 那层：Service 的实现类自己绑的表反而没被走到
    assert "OrderMapper(mapper)" in result["owners"]


def test_a_class_bound_to_an_entity_without_a_table_name_claims_nothing() -> None:
    """实体没有 @TableName 时不许拿类名去当表名猜。"""

    text = ENTITY.replace('@TableName("t_order")\n', "")
    cls = parse_java(text, "Order.java")

    assert cls is not None
    assert cls.table_name == ""


def test_an_upsert_tail_is_not_mistaken_for_a_table_name() -> None:
    """`ON DUPLICATE KEY UPDATE id = id` 里的 update 后面是列名，不是表。"""

    namespace, statements = parse_mapper(
        """<?xml version="1.0"?>
<mapper namespace="com.demo.rate.RateShareStoreMapper">
  <insert id="ensureRelations">
    INSERT INTO prc_rate_share_relation (id, source_rate_id, tenant_id)
    VALUES (#{row.id}, #{row.sourceRateId}, #{row.tenantId})
    ON DUPLICATE KEY UPDATE id = id
  </insert>
</mapper>
""",
        "RateShareStoreMapper.xml",
    )

    assert namespace == "com.demo.rate.RateShareStoreMapper"
    assert statements[0][2] == ["prc_rate_share_relation"]
