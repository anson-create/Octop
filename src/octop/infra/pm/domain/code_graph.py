"""代码链路图谱：HTTP 接口 -> Controller -> Service -> Mapper -> 数据库表。

只回答一件事：**改动某个接口会碰到哪些表**。不做完整符号索引，也不追求
语法级正确 —— 目标是 2 核机器上十分钟内跑完 2 万个 java 文件，并用真实
代码抽查过。

为什么不是通用符号索引：需求评审要的是「这个功能读写哪几张表」，而这句话
的答案在 Java 里被拆成四段——注解给出接口、字段注入给出下一跳、接口到实现
类靠 implements、最后一段在 Mapper XML 的 SQL 里。把这四段连起来就是一张
很窄的图，窄到 SQLite 一张表放得下，也窄到不必引图数据库。

用法：
    python -m octop.infra.pm.domain.code_graph --root /opt/pm-mirror/cwx-api
    python -m octop.infra.pm.domain.code_graph --root ... --query "POST /billing/list"
    python -m octop.infra.pm.domain.code_graph --selftest /path/to/SomeController.java
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 一个源文件两个视图：raw 和 masked 等长。masked 把注释和字符串字面量抹成
# 空格，用来判断花括号结构；需要取注解里的路径、SQL 里的表名时回到 raw 的
# 同一偏移去读。不这么做的话，@RequestMapping("/billing") 里的路径会被当成
# 代码，而方法体里注释掉的 { } 会把整个类的边界算错。
# --------------------------------------------------------------------------

_STR = ('"', "'")


def mask(src: str) -> str:
    out = list(src)
    index, size = 0, len(src)
    while index < size:
        char = src[index]
        if char == "/" and index + 1 < size:
            nxt = src[index + 1]
            if nxt == "/":
                end = src.find("\n", index)
                end = size if end < 0 else end
                for pos in range(index, end):
                    out[pos] = " "
                index = end
                continue
            if nxt == "*":
                end = src.find("*/", index + 2)
                end = size if end < 0 else end + 2
                for pos in range(index, end):
                    if out[pos] != "\n":
                        out[pos] = " "
                index = end
                continue
        if char in _STR:
            # Java 15 text block: """ ... """
            if src.startswith('"""', index):
                end = src.find('"""', index + 3)
                end = size if end < 0 else end + 3
            else:
                end = index + 1
                while end < size:
                    if src[end] == "\\":
                        end += 2
                        continue
                    if src[end] == char:
                        end += 1
                        break
                    end += 1
            for pos in range(index, min(end, size)):
                if out[pos] != "\n":
                    out[pos] = " "
            index = end
            continue
        index += 1
    return "".join(out)


def _braces(masked: str, start: int) -> int:
    """返回从 start 起第一个 '{' 对应的 '}' 之后的位置；没有则 -1。"""
    depth = 0
    for pos in range(start, len(masked)):
        char = masked[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return pos + 1
    return -1


def _first_brace(masked: str, start: int, limit: int) -> int:
    for pos in range(start, limit):
        if masked[pos] == "{":
            return pos
    return -1


_ANN = re.compile(r"@([A-Za-z_][\w.]*)\s*(?:\((?:[^()]|\([^()]*\))*\))?")
# extends/implements 都可能带泛型：`interface XxxMapper extends BaseMapper<Invoice>`
# 少了 <...> 这一段，全仓 800 多个 MyBatis-Plus 的 Mapper 会整类解析失败。
_CLASS_DECL = re.compile(
    r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s*<[^{;]*?>)?"
    r"(?:\s+extends\s+([\w.]+)(?:\s*<[^{;]*?>)?)?"
    r"(?:\s+implements\s+([\w.,\s<>]+?))?\s*\{",
    re.S,
)
_FIELD = re.compile(
    r"(?:(?:private|protected|public|static|final|transient|volatile)\s+)*"
    r"([A-Z][\w.$]*(?:\s*<[^;{}()]*>)?(?:\[\])?)\s+"
    r"([a-z_$][\w$]*)\s*(?:=[^;{}]*)?$",
    re.S,
)
_CALL = re.compile(r"\b(?:this\s*\.\s*)?([a-z_$][\w$]*)\s*\.\s*([a-z_$][\w$]*)\s*\(")
_MAPPING = {
    "PostMapping": "POST",
    "GetMapping": "GET",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "",
}
_HTTP_HINT = re.compile(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)")

_SQL_TABLE = re.compile(
    r"\b(?:from|join|update|insert\s+into|into)\s+"
    r"([A-Za-z_][\w$]*\.[A-Za-z_][\w$]*|[A-Za-z_][\w$]*)",
    re.I,
)
_SQL_SKIP = {
    "select", "where", "set", "values", "dual", "on", "as", "and", "or",
    "left", "right", "inner", "outer", "cross", "union", "all", "distinct",
    "if", "foreach", "trim", "choose", "when", "otherwise", "each", "bind",
    "include", "sql", "where_clause", "set_clause",
}

# 内联 SQL：JdbcTemplate、@Select 注解、以及手拼的 Statement。这类代码不走
# Mapper XML，只从 XML 抽表名会把它们整个漏掉——备份模块 4 个 Service 全是
# 这种写法，图谱上看起来「这个接口不碰任何表」，而它明明写了
# ``SELECT 1 FROM bkp_routing_registry``。
_STR_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
# 只有同时出现动词和宾语才算 SQL。日志里的 "loaded from cache" 会被
# ``from`` 单独骗到，加动词后不会再进。
_SQL_SHAPE = re.compile(
    r"(?:\bselect\b[\s\S]{0,4000}?\bfrom\b)"
    r"|(?:\binsert\s+into\b)"
    r"|(?:\bdelete\s+from\b)"
    r"|(?:\bupdate\s+[A-Za-z_][\w$]*\s+set\b)",
    re.I,
)


def inline_sql_tables(segment: str) -> list[tuple[str, list[str]]]:
    """方法体或注解参数里的内联 SQL 碰到哪些表。

    参数拼进去的表名（``"SELECT * FROM " + table``）抽不到，这是静态解析的
    边界，不是 bug——宁可少一张，不能猜一张。
    """

    found: list[tuple[str, list[str]]] = []
    for literal in _STR_LITERAL.finditer(segment):
        sql = literal.group(1)
        if len(sql) < 16 or not _SQL_SHAPE.search(sql):
            continue
        head = sql.lstrip()[:16].lower()
        if head.startswith("select"):
            kind = "select"
        elif head.startswith("insert"):
            kind = "insert"
        elif head.startswith("update"):
            kind = "update"
        elif head.startswith("delete"):
            kind = "delete"
        else:
            kind = "sql"
        tables: list[str] = []
        for table in _SQL_TABLE.finditer(sql):
            name = table.group(1).split(".")[-1]
            if name.lower() in _SQL_SKIP or len(name) < 2:
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            if name not in tables:
                tables.append(name)
        if tables:
            found.append((kind, tables))
    return found


@dataclass
class Method:
    name: str
    argc: int
    line: int
    http: str = ""
    path: str = ""
    calls: list[str] = field(default_factory=list)  # 被调方法的简单名
    owner: str = ""
    sql_tables: list[tuple[str, list[str]]] = field(default_factory=list)
    body: tuple[int, int] | None = None


@dataclass
class JavaClass:
    rel: str
    package: str
    name: str
    kind: str
    extends: str = ""
    implements: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    base_path: str = ""
    controller: bool = False
    # @FeignClient 是**调用方**的声明，不是被调方的端点。它长得和 Controller
    # 一模一样（类上 @RequestMapping、方法上 @PostMapping），但没有方法体，
    # 顺着它永远走不到 Mapper——把它当端点会让「接口能不能追到表」的分母凭空
    # 多出四分之一，而这些端点注定追不到。
    feign: bool = False
    fields: dict[str, str] = field(default_factory=dict)
    methods: list[Method] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        return f"{self.package}.{self.name}" if self.package else self.name


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _join_path(base: str, sub: str) -> str:
    """类级前缀 + 方法级路径。`POST list` 是废路径，必须拼成 /billing/list。"""
    parts = [part.strip("/") for part in (base, sub) if part and part.strip("/")]
    joined = "/".join(parts)
    return "/" + joined if joined else "/"


def _ann_path(raw: str, start: int, end: int) -> str:
    """注解参数里的第一个字符串字面量（回到原文取，masked 里已被抹掉）。"""
    chunk = raw[start:end]
    found = re.search(r'"([^"]*)"', chunk)
    return found.group(1) if found else ""


def parse_java(raw: str, rel: str) -> JavaClass | None:
    masked = mask(raw)
    decl = _CLASS_DECL.search(masked)
    if decl is None:
        return None

    kind, name = decl.group(1), decl.group(2)
    pkg = re.search(r"\bpackage\s+([\w.]+)\s*;", masked)
    cls = JavaClass(
        rel=rel,
        package=pkg.group(1) if pkg else "",
        name=name,
        kind=kind,
        extends=(decl.group(3) or "").split(".")[-1],
        implements=[
            part.strip().split("<")[0].split(".")[-1]
            for part in (decl.group(4) or "").split(",")
            if part.strip()
        ],
    )

    # 类级注解：从文件开头到类声明之间的所有注解
    head = masked[: decl.start()]
    for ann in _ANN.finditer(head):
        cls.annotations.append(ann.group(1).split(".")[-1])
        if ann.group(1).split(".")[-1] in _MAPPING:
            cls.base_path = _ann_path(raw, ann.start(), ann.end())
    cls.controller = any(
        name in {"RestController", "Controller"} for name in cls.annotations
    )
    # 只认注解，不认类名。叫 XxxFeign 但没有 @FeignClient 的接口确实存在，
    # 按名字猜会把一个真实 Controller 踢出分母，那种错没法从数字上看出来。
    cls.feign = any(name == "FeignClient" for name in cls.annotations)

    body_start = decl.end() - 1  # 指向 '{'
    body_end = _braces(masked, body_start)
    if body_end < 0:
        body_end = len(masked)

    # 类体内深度为 1 的成员逐个切开
    cursor = body_start + 1
    seg_start = cursor
    while cursor < body_end:
        char = masked[cursor]
        if char == ";":
            _member(cls, raw, masked, seg_start, cursor)
            seg_start = cursor + 1
            cursor += 1
            continue
        if char == "{":
            end = _braces(masked, cursor)
            if end < 0:
                break
            _member(cls, raw, masked, seg_start, cursor, body_end=end)
            cursor = end
            seg_start = end
            continue
        if char == "}":
            break
        cursor += 1
    _local_calls(cls, masked)
    return cls


def _member(
    cls: JavaClass,
    raw: str,
    masked: str,
    start: int,
    end: int,
    body_end: int | None = None,
) -> None:
    """一段成员声明：以 ';' 结束的是字段或抽象方法，以 '{' 开始的是有体方法。"""
    decl = masked[start:end]
    if not decl.strip():
        return
    anns = list(_ANN.finditer(decl))
    sig = _ANN.sub(" ", decl).strip()
    if not sig:
        return

    if body_end is None:
        # 字段，或接口里的抽象方法
        if "(" in sig:
            _method(cls, raw, masked, start, end, anns, sig, None)
            return
        field_match = _FIELD.search(sig)
        if field_match:
            type_name = field_match.group(1).split("<")[0].strip()
            cls.fields[field_match.group(2)] = type_name.split(".")[-1]
        return
    _method(cls, raw, masked, start, end, anns, sig, body_end)


def _method(
    cls: JavaClass,
    raw: str,
    masked: str,
    start: int,
    sig_end: int,
    anns: list[re.Match[str]],
    sig: str,
    body_end: int | None,
) -> None:
    paren = sig.find("(")
    if paren < 0:
        return
    name_match = re.search(r"([A-Za-z_$][\w$]*)\s*$", sig[:paren])
    if name_match is None:
        return
    name = name_match.group(1)
    if name in {cls.name, "if", "for", "while", "switch", "catch", "return"}:
        return

    # 参数个数：要跳过泛型里的逗号
    depth, argc, seen = 0, 0, False
    for char in sig[paren + 1 :]:
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            argc += 1
        elif not char.isspace():
            seen = True
    if seen:
        argc += 1

    method = Method(name=name, argc=argc, line=_line_of(raw, start), owner=cls.fqn)

    for ann in anns:
        simple = ann.group(1).split(".")[-1]
        if simple not in _MAPPING:
            continue
        text = ann.group(0)
        hint = _HTTP_HINT.search(text)
        method.http = _MAPPING[simple] or (hint.group(1) if hint else "ANY")
        method.path = _join_path(cls.base_path, _ann_path(raw, ann.start() + start, ann.end() + start))
        break

    if body_end is not None:
        method.body = (sig_end, body_end)
        body = masked[sig_end:body_end]
        for call in _CALL.finditer(body):
            target = cls.fields.get(call.group(1))
            if target is not None:
                method.calls.append(f"{target}#{call.group(2)}")
    # 注解参数也在这一段里：@Select("select ... from t") 是方法级的写法，
    # 只扫方法体会漏掉。
    method.sql_tables = inline_sql_tables(raw[start : body_end or sig_end])
    cls.methods.append(method)


# 无前缀的本类调用。`existsInRegistry(...)` 前面没有 ``this.`` 也没有字段名，
# 光靠「字段名.方法名」会漏掉整条链路——备份模块的 JdbcTemplate SQL 就藏在
# 这种私有辅助方法里。
_LOCAL_CALL = re.compile(r"(?:^|[^\w.$])([a-z_$][\w$]*)\s*\(")
_NOT_A_CALL = {
    "if", "for", "while", "switch", "catch", "return", "new", "super", "this",
    "assert", "synchronized", "throw", "try", "do", "else", "instanceof",
    "case", "break", "continue", "yield", "var", "record", "with", "when",
}


def _local_calls(cls: JavaClass, masked: str) -> None:
    """把本类内部的调用补进方法节点，让私有辅助方法接得上。"""

    names = {method.name for method in cls.methods}
    for method in cls.methods:
        if method.body is None:
            continue
        body = masked[method.body[0] : method.body[1]]
        for call in _LOCAL_CALL.finditer(body):
            name = call.group(1)
            if name in _NOT_A_CALL or name == method.name or name not in names:
                continue
            edge = f"{cls.name}#{name}"
            if edge not in method.calls:
                method.calls.append(edge)


# --------------------------------------------------------------------------
# MyBatis XML
# --------------------------------------------------------------------------

_XML_STMT = re.compile(
    r"<(select|insert|update|delete)\b[^>]*\bid\s*=\s*\"([^\"]+)\"[^>]*>(.*?)</\1>",
    re.S | re.I,
)
_XML_NS = re.compile(r"namespace\s*=\s*\"([^\"]+)\"")


def parse_mapper(raw: str, rel: str) -> tuple[str, list[tuple[str, str, list[str]]]]:
    ns_match = _XML_NS.search(raw)
    namespace = ns_match.group(1) if ns_match else ""
    statements: list[tuple[str, str, list[str]]] = []
    for stmt in _XML_STMT.finditer(raw):
        kind = stmt.group(1).lower()
        stmt_id = stmt.group(2)
        body = stmt.group(3)
        tables: list[str] = []
        for table in _SQL_TABLE.finditer(body):
            name = table.group(1)
            short = name.split(".")[-1]
            if short.lower() in _SQL_SKIP or not short:
                continue
            if short not in tables:
                tables.append(short)
        statements.append((stmt_id, kind, tables))
    return namespace, statements


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


@dataclass
class Graph:
    # 以全限定名做键。用简单名做键时，两个包里同名的那对类会互相覆盖——
    # 10918 个文件只建出 8766 个类，差值里大部分是这么丢的。
    classes: dict[str, JavaClass] = field(default_factory=dict)
    impls: dict[str, list[str]] = field(default_factory=dict)
    mappers: dict[str, list[tuple[str, str, list[str]]]] = field(default_factory=dict)

    def add(self, cls: JavaClass) -> None:
        self.classes[cls.fqn] = cls
        for interface in cls.implements:
            self.impls.setdefault(interface, []).append(cls.fqn)
        # 内联 SQL 的表挂在「类名.方法名」上，正好和 Mapper 的
        # 「namespace 短名.方法名」同形，追溯时不必区分这两种来源。
        for method in cls.methods:
            if not method.sql_tables:
                continue
            bucket = self.mappers.setdefault(cls.name, [])
            for kind, tables in method.sql_tables:
                bucket.append((method.name, kind, tables))


def walk_roots(roots: list[str], limit: int = 0) -> Graph:
    graph = Graph()
    seen = 0
    started = time.time()
    xml_files = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            # 目标目录、构建产物、测试源码都不进图谱
            dirnames[:] = [
                item
                for item in dirnames
                if item not in {"target", "node_modules", ".git", "build", "out", "test"}
            ]
            if "/src/test" in dirpath.replace(os.sep, "/"):
                continue
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, root)
                if filename.endswith(".java"):
                    seen += 1
                    if limit and seen > limit:
                        return graph
                    try:
                        with open(full, encoding="utf-8", errors="replace") as handle:
                            raw = handle.read()
                    except OSError:
                        continue
                    cls = parse_java(raw, rel)
                    if cls is not None:
                        graph.add(cls)
                    if seen % 2000 == 0:
                        print(
                            f"  ...{seen} java, {len(graph.classes)} 类, "
                            f"{time.time() - started:.0f}s",
                            file=sys.stderr,
                            flush=True,
                        )
                elif filename.endswith(".xml") and "mapper" in full.lower():
                    xml_files += 1
                    try:
                        with open(full, encoding="utf-8", errors="replace") as handle:
                            raw = handle.read()
                    except OSError:
                        continue
                    namespace, statements = parse_mapper(raw, rel)
                    if namespace and statements:
                        graph.mappers[namespace.split(".")[-1]] = statements
    print(
        f"  解析完成：{seen} java / {len(graph.classes)} 类 / "
        f"{xml_files} mapper xml / {len(graph.mappers)} 命名空间 / "
        f"{time.time() - started:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    return graph


# --------------------------------------------------------------------------
# 落库与查询
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoint (
  ep TEXT PRIMARY KEY, http TEXT, path TEXT, cls TEXT, method TEXT, rel TEXT
);
CREATE TABLE IF NOT EXISTS edge (
  src TEXT, dst TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS tbl (
  statement TEXT, table_name TEXT, kind TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_edge ON edge(src, dst, kind);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON edge(dst);
CREATE INDEX IF NOT EXISTS ix_tbl_stmt ON tbl(statement);
"""


def _method_node(fqn: str, name: str) -> str:
    return f"m:{fqn}#{name}"


def build_edges(
    graph: Graph,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str]]]:
    """返回 (边, 端点)。端点：(接口, 类全名, 方法名, 源文件)。

    方法节点不带参数个数：在「这个接口会碰到哪张表」这个问题上重载应当合并，
    少写一个 argc 换来的是不会因为 ``save(String)`` 和 ``save(String, int)``
    分别建点而丢掉一半的表。

    @FeignClient 的映射只建边、不建端点：它是调用方的声明，真正的实现在别的
    服务里，顺着它走不到任何表。
    """

    edges: list[tuple[str, str, str]] = []
    endpoints: list[tuple[str, str, str, str]] = []
    for cls in graph.classes.values():
        if cls.extends:
            edges.append((f"cls:{cls.fqn}", f"cls:{cls.extends}", "extends"))
        for interface in cls.implements:
            edges.append((f"cls:{cls.fqn}", f"cls:{interface}", "implements"))
        for method in cls.methods:
            src = _method_node(cls.fqn, method.name)
            for target in method.calls:
                edges.append((src, f"m:{target}", "call"))
            if method.http and not cls.feign:
                ep = f"{method.http} {method.path}"
                endpoints.append((ep, cls.fqn, method.name, cls.rel))
                edges.append((f"ep:{ep}", src, "handles"))
    return edges, endpoints


def store(graph: Graph, out: str) -> dict[str, int]:
    if os.path.exists(out):
        os.remove(out)
    conn = sqlite3.connect(out)
    conn.executescript(SCHEMA)
    edges, endpoints = build_edges(graph)

    conn.executemany(
        "INSERT OR REPLACE INTO endpoint(ep,http,path,cls,method,rel) VALUES(?,?,?,?,?,?)",
        [(ep, *ep.split(" ", 1), cls, method, rel) for ep, cls, method, rel in endpoints],
    )
    conn.executemany("INSERT OR IGNORE INTO edge(src,dst,kind) VALUES(?,?,?)", edges)
    rows = []
    for mapper, statements in graph.mappers.items():
        for stmt_id, kind, tables in statements:
            for table in tables:
                rows.append((f"{mapper}.{stmt_id}", table, kind))
    conn.executemany("INSERT INTO tbl(statement,table_name,kind) VALUES(?,?,?)", rows)
    conn.commit()

    counts = {
        "classes": len(graph.classes),
        "mappers": len(graph.mappers),
        "endpoints": len(endpoints),
        "edges": len(edges),
        "table_refs": len(rows),
    }
    conn.close()
    return counts


def trace(conn: sqlite3.Connection, endpoint: str, max_depth: int = 8) -> dict[str, object]:
    """从接口出发，沿 call 走到 Mapper 方法，再用 XML statement 换成表名。

    一跳一个可核对的事实：字段注入说明它调谁，implements 说明接口的实现在哪，
    XML 的 namespace.id 说明这条 SQL 碰哪张表。任何一跳缺失只会让表少几张，
    不会凭空给出一张不存在的表——宁可少说，不能说错。
    """

    row = conn.execute(
        "SELECT cls, method, rel FROM endpoint WHERE ep = ?", (endpoint,)
    ).fetchone()
    if row is None:
        return {"found": False, "endpoint": endpoint}
    cls, method, rel = row

    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(_method_node(cls, method), 0)]
    tables: set[str] = set()
    statements: set[str] = set()
    walked: list[str] = []
    while queue:
        node, depth = queue.pop(0)
        if node in seen or depth > max_depth:
            continue
        seen.add(node)
        walked.append(node)
        for (dst,) in conn.execute(
            "SELECT dst FROM edge WHERE src = ? AND kind = 'call'", (node,)
        ):
            queue.append((dst, depth + 1))
            target_fqn, _, target_method = dst[2:].partition("#")
            # 接口声明的方法没有方法体，真正干活的是实现类。把实现类的同名方法
            # 也入队，才接得上 Mapper 那一跳。
            for (impl,) in conn.execute(
                "SELECT src FROM edge WHERE dst = ? AND kind = 'implements'",
                (f"cls:{target_fqn}",),
            ):
                queue.append((_method_node(impl[4:], target_method), depth + 1))
            statement = f"{target_fqn.split('.')[-1]}.{target_method}"
            hit = conn.execute(
                "SELECT DISTINCT table_name FROM tbl WHERE statement = ?", (statement,)
            ).fetchall()
            if hit:
                statements.add(statement)
                tables.update(name for (name,) in hit)
    return {
        "found": True,
        "endpoint": endpoint,
        "rel": rel,
        "nodes": len(seen),
        "statements": sorted(statements),
        "tables": sorted(tables),
    }


def coverage(conn: sqlite3.Connection) -> dict[str, int]:
    """有多少接口能追到至少一张表。

    这是这个图谱唯一的诚实体检项：接口总数减去追到的数量，就是「问了也答
    不上来」的比例。它不会因为边变多而自动变好，只会在真的接通一条链路时
    才动。
    """

    total = conn.execute("SELECT COUNT(*) FROM endpoint").fetchone()[0]
    rows = conn.execute("SELECT ep FROM endpoint").fetchall()
    answered = sum(1 for (ep,) in rows if trace(conn, ep)["tables"])
    return {"endpoints": total, "with_tables": answered, "silent": total - answered}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", default=[])
    parser.add_argument("--out", default="/tmp/code-graph.sqlite")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--query-only", action="store_true", help="不重建，只查已有的库")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--selftest", default="")
    args = parser.parse_args()

    if args.selftest:
        with open(args.selftest, encoding="utf-8", errors="replace") as handle:
            cls = parse_java(handle.read(), os.path.basename(args.selftest))
        if cls is None:
            print("没有解析出类")
            return 1
        print(f"{cls.kind} {cls.fqn}  controller={cls.controller}  base={cls.base_path}")
        print(f"  implements={cls.implements}  字段={len(cls.fields)}  方法={len(cls.methods)}")
        for name, type_name in list(cls.fields.items())[:8]:
            print(f"    字段 {type_name} {name}")
        for method in cls.methods:
            if method.http:
                print(f"    {method.http:6} {method.path:40} {method.name}/{method.argc}")
                for call in method.calls[:6]:
                    print(f"          -> {call}")
        return 0

    # --coverage 只说明「建完顺便报个体检结果」，不该因此跳过建图。只有明确
    # 说 --query-only、或者根本没给 --root 时才读旧库。
    rebuild = bool(args.root) and not args.query_only
    if rebuild:
        started = time.time()
        graph = walk_roots(args.root, limit=args.limit)
        counts = store(graph, args.out)
        size = os.path.getsize(args.out) / 1024 / 1024
        print(f"\n图谱：{args.out}  ({size:.1f} MB, {time.time() - started:.0f}s)")
        for key, value in counts.items():
            print(f"  {key:12} {value}")
    elif not os.path.exists(args.out):
        print(f"图谱不存在：{args.out}")
        return 1

    conn = sqlite3.connect(args.out)
    if args.coverage:
        stats = coverage(conn)
        rate = stats["with_tables"] / max(stats["endpoints"], 1) * 100
        print(
            f"\n覆盖：{stats['with_tables']}/{stats['endpoints']} 个接口能追到表"
            f"（{rate:.1f}%），{stats['silent']} 个追不到"
        )
    for endpoint in args.query:
        result = trace(conn, endpoint)
        print(f"\n{endpoint}")
        if not result["found"]:
            print("  图谱里没有这个接口")
            continue
        print(f"  文件 {result['rel']}  走过 {result['nodes']} 个节点")
        print(f"  statement {result['statements'] or '（无）'}")
        print(f"  表 {result['tables'] or '（无）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
