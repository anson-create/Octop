"""What a Jira component actually names — read from the router, not guessed.

A component called "订单中心" is not a page, it is a section of the product, and
the product already says so: the Vue Router config carries ``meta.title`` in the
same Chinese the component uses. On the real CargoWareX mirror, 8 of 14
components match a route title exactly and uniquely. That is a far better anchor
than any keyword search over source text, because it is the product's own
vocabulary rather than our guess at it.

Two things this deliberately does not do:

* Resolve duplicates. 99 titles appear more than once, usually because the same
  menu name exists in the tenant app and the admin app. Those are genuinely
  different pages, so the caller gets every match and has to decide.
* Pretend a section is a file. ``/basic-data`` renders ``ports/index.vue`` only
  because that is the first child; the section has dozens of pages. Such rows are
  flagged, so nobody derives a prototype from the ports screen when the ticket
  was about ports *and* warehouses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_IMPORT_RE = re.compile(
    r"""import\(\s*['"]([^'"]+)['"]\s*\)|import\s+[\w{}',\s*]+\s+from\s+['"]([^'"]+)['"]"""
)
_TITLE_RE = re.compile(r"""title:\s*['"]([^'"]+)['"]""")
_PATH_RE = re.compile(r"""path:\s*['"]([^'"]*)['"]""")
_COMPONENT_RE = re.compile(r"""component:\s*""")

# macOS duplicate copies ("routes 2.ts") and build output must never be parsed:
# they would inject routes that no running app mounts.
_SKIP_NAME_RE = re.compile(r"(\s\d+)\.ts$|\.d\.ts$")
_SKIP_DIRS = ("node_modules", "/dist/", "/.git/")

# Only .vue/tsx files are renderable pages; a redirect or a layout is not.
_SECTION_HINT = ("layout",)


@dataclass(frozen=True)
class RouteEntry:
    """One route that names a page in Chinese."""

    title: str
    path: str
    app: str
    component: str  # as written, e.g. @/views/order/index.vue
    file: str  # resolved, relative to the app's src root
    route_file: str
    # The grouping the route belongs to: a sub-app in a monorepo, or the router
    # module in a single-app repo where ``router/modules/<Module>/`` *is* the
    # section. Two routes with the same scope are in the same functional area.
    scope: str = ""
    # True when other routes live under this path: the title names a section,
    # and the component is only that section's default child.
    section: bool = False
    # Bytes on disk. An entry under ~1KB is an entrance shell that imports the
    # real page one level down (``order/index.vue`` -> ``OrderListUnified.vue``).
    # Harmless for rendering, but it tells a reviewer they are looking at a
    # wrapper rather than the screen itself.
    size: int = 0
    # A layout is a frame, not a screen. Several apps route "/" to one and title
    # it 首页, which otherwise competes with the real home page.
    layout: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "path": self.path,
            "app": self.app,
            "component": self.component,
            "file": self.file,
            "scope": self.scope,
            "section": self.section,
            "size": self.size,
            "layout": self.layout,
        }


_COMPOUND_SPLIT = re.compile(r"[-－—_/]")


@dataclass(frozen=True)
class Match:
    """The outcome of asking "which page is this component?"."""

    entry: RouteEntry | None = None
    candidates: tuple[RouteEntry, ...] = ()
    # exact | compound | ambiguous | none
    reason: str = "none"

    @property
    def ok(self) -> bool:
        return self.entry is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "page": self.entry.file if self.entry else "",
            "app": self.entry.app if self.entry else "",
            "candidates": [item.as_dict() for item in self.candidates],
        }


def _objects(text: str):
    """Yield balanced ``{...}`` substrings.

    A regex cannot bound a route object: ``meta`` nests braces, and a lazy
    pattern runs past the closing brace into the next route, pairing this
    route's component with the next route's title.
    """

    stack: list[int] = []
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            stack.append(index)
        elif char == "}" and stack:
            yield text[stack.pop() : index + 1]


def _route_files(app_src: Path) -> list[Path]:
    """Every router file under ``src/router``, at any depth.

    Vue 3 repos keep one ``router/index.ts``; Vue 2 repos split a file per
    business module (``router/modules/cargo/v0/SeaExport/index.js``). Stopping at
    the first level reads the Vue 3 layout and silently finds nothing on the
    Vue 2 one.
    """

    router = app_src / "router"
    if not router.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(router.rglob("*")):
        if not path.is_file() or path.suffix not in (".ts", ".js"):
            continue
        text = str(path)
        if _SKIP_NAME_RE.search(path.name) or any(marker in text for marker in _SKIP_DIRS):
            continue
        out.append(path)
    return out


def _scope_of(route_file: Path, app_src: Path, app: str) -> str:
    """The functional area a route belongs to.

    In a monorepo it is the sub-app. In a single-app repo the router modules are
    the sections, and the directory is authoritative: ``SystemMaintenance/``
    holds every 系统维护 route, which is what a
    ``系统维护-文档模板管理`` component is really naming.
    """

    try:
        parts = route_file.relative_to(app_src / "router").parts
    except ValueError:
        return app
    if len(parts) >= 2:
        # modules/cargo/v0/SeaExport/index.js -> SeaExport
        return parts[-2] if parts[-1].startswith("index.") else route_file.stem
    return app


def _relative_to_src(spec: str) -> str:
    """``@/views/x`` and ``@views/x`` both mean the app's src directory.

    The mirror really does contain the typo'd form — ``@views/customer/...`` —
    so a strict ``@/`` strip would leave a leading ``@`` and resolve to a file
    that does not exist.
    """

    if spec.startswith("@/"):
        return spec[2:]
    if spec.startswith("@views/"):
        return "views/" + spec[len("@views/") :]
    return spec.lstrip("@")


def _is_layout(relative: str) -> bool:
    """A frame component, not a screen.

    Only the ``layouts/`` directory counts. The singular ``layout`` is the shell
    every page sits inside, and its route carries the section's own title —
    marking that as a layout would delete the section from the index.
    """

    parts = relative.lower().split("/")
    if parts[0] == "layouts":
        return True
    return parts[-1].endswith("layout.vue")


_PAGE_EXTENSIONS = (".vue", ".tsx", ".jsx", ".ts", ".js")


def _resolve_page(app_src: Path, spec: str) -> tuple[str, int]:
    """The real file behind a route's component spec, plus its size.

    Vue 2 writes ``@/views/cargo/v0/FBA/fbaOrder`` and lets the bundler append
    the extension, so the spec is not a path. Handing it to the deriver finds
    nothing, and the size check silently reads 0 for every page.
    """

    base = app_src / spec
    if base.suffix in _PAGE_EXTENSIONS and base.is_file():
        return spec, base.stat().st_size
    for candidate in (
        f"{spec}.vue",
        f"{spec}/index.vue",
        f"{spec}.tsx",
        f"{spec}.jsx",
        f"{spec}/index.tsx",
    ):
        target = app_src / candidate
        if target.is_file():
            return candidate, target.stat().st_size
    return spec, 0


def _parse_route_file(
    path: Path, *, app: str, app_src: Path, scope: str = ""
) -> list[RouteEntry]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rows: list[RouteEntry] = []
    for chunk in _objects(text):
        if not _COMPONENT_RE.search(chunk):
            continue
        title = _TITLE_RE.search(chunk)
        if title is None:
            continue
        spec = ""
        for match in _IMPORT_RE.finditer(chunk):
            spec = match.group(1) or match.group(2) or ""
            if spec:
                break
        if not spec:
            continue
        route_path = _PATH_RE.search(chunk)
        relative = _relative_to_src(spec)
        resolved, size = _resolve_page(app_src, relative)
        rows.append(
            RouteEntry(
                title=title.group(1).strip(),
                path=route_path.group(1) if route_path else "",
                app=app,
                component=spec,
                file=resolved,
                route_file=str(path),
                scope=scope,
                size=size,
                layout=_is_layout(resolved),
            )
        )
    return rows


class RouteIndex:
    """Routes across every sub-app of a front-end monorepo, keyed by title."""
    def __init__(
        self,
        entries: list[RouteEntry],
        *,
        root: Path | None = None,
        single: bool = False,
    ) -> None:
        self._entries = entries
        self.root = root
        self._single = single
        # When every scope is just its app, the repo is a monorepo and area has
        # to be judged by route path. Otherwise the router modules are the areas.
        self._scope_is_app = all(entry.scope == entry.app for entry in entries)
        self._by_title: dict[str, list[RouteEntry]] = {}
        for entry in entries:
            self._by_title.setdefault(entry.title, []).append(entry)
        self._mark_sections()

    def repo_path(self, entry: RouteEntry) -> str:
        """The entry's file as the deriver wants it: relative to the repo root.

        ``RouteEntry.file`` is relative to the app's ``src`` because that is what
        the ``@/`` alias means. The renderer reads ``root / page`` and hands the
        same string to Vite, so handing it the alias-relative form finds no file.
        """

        if not entry.file:
            return ""
        return f"src/{entry.file}" if self._single else f"{entry.app}/src/{entry.file}"

    def _mark_sections(self) -> None:
        """A title whose path prefixes another route is a section, not a page."""

        marked: list[RouteEntry] = []
        for entry in self._entries:
            if not entry.path:
                marked.append(entry)
                continue
            prefix = entry.path.rstrip("/") or "/"
            nested = any(
                other.path.rstrip("/") != prefix and other.path.rstrip("/").startswith(prefix)
                for other in self._entries
                if other.app == entry.app
            )
            marked.append(
                RouteEntry(
                    title=entry.title,
                    path=entry.path,
                    app=entry.app,
                    component=entry.component,
                    file=entry.file,
                    route_file=entry.route_file,
                    scope=entry.scope,
                    section=nested,
                    size=entry.size,
                    layout=entry.layout,
                )
            )
        self._entries = marked
        self._by_title = {}
        for entry in marked:
            self._by_title.setdefault(entry.title, []).append(entry)

    @classmethod
    def load(cls, repo_root: Path, *, apps: tuple[str, ...] | None = None) -> RouteIndex | None:
        """Parse every router. ``None`` when the repo has no router at all.

        Two layouts, told apart by where ``router/`` sits: a monorepo puts one
        under each sub-app, a single-app repo puts one at ``src/router``.
        """

        root = Path(repo_root)
        if not root.is_dir():
            return None
        single = apps is None and (root / "src" / "router").is_dir()
        if single:
            targets: list[tuple[str, Path]] = [(root.name, root / "src")]
        else:
            names = apps or tuple(
                child.name
                for child in sorted(root.iterdir())
                if child.is_dir() and (child / "src" / "router").is_dir()
            )
            targets = [(name, root / name / "src") for name in names]
        entries: list[RouteEntry] = []
        for app, app_src in targets:
            for route_file in _route_files(app_src):
                entries.extend(
                    _parse_route_file(
                        route_file,
                        app=app,
                        app_src=app_src,
                        scope=_scope_of(route_file, app_src, app),
                    )
                )
        if not entries:
            return None
        return cls(entries, root=root, single=single)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def titles(self) -> set[str]:
        return set(self._by_title)

    def lookup(self, title: str) -> list[RouteEntry]:
        """Every route whose title is exactly this. Empty list means no match."""

        return list(self._by_title.get((title or "").strip(), ()))

    def resolve(
        self, title: str, *, prefer: tuple[str, ...] = ()
    ) -> tuple[RouteEntry | None, list[RouteEntry]]:
        """``(the_one, candidates)`` — the_one is None unless it is unambiguous.

        Unambiguous means exactly one route carries the title. Two routes with
        the same title in two apps are two different pages, and choosing either
        would be a confident guess.

        ``prefer`` is the one exception, and it is a product fact rather than a
        tie-break heuristic: a CargoWareX requirement is a *tenant* requirement,
        so when 系统设置 exists in both the tenant app and the platform-admin
        back office, the tenant one is the page the ticket is about. It is an
        ordered list, and it never manufactures a match — an app with two
        same-titled routes is still ambiguous, and an app that does not carry
        the title falls through to the next.
        """

        rows = self.lookup(title)
        # Layouts stay in ``rows`` as evidence but never win a tie: several apps
        # route "/" to a frame and title it 首页, which would otherwise compete
        # with the real home page.
        viable = [row for row in rows if not row.layout] or rows
        if len(viable) == 1:
            return viable[0], rows
        if len(viable) > 1:
            # Two routes onto the same file are one screen reached two ways —
            # an alias, not a choice. Counting them as a tie would mark a
            # perfectly answerable component as needing review.
            if len({row.file for row in viable}) == 1:
                return viable[0], rows
            for app in prefer:
                picked = [row for row in viable if row.app == app]
                if len(picked) == 1:
                    return picked[0], rows
                if picked:
                    # Two routes inside the app we were told to prefer: the
                    # preference cannot settle that.
                    return None, rows
        return None, rows

    def match(self, title: str, *, prefer: tuple[str, ...] = ()) -> Match:
        """Locate a component, including the ``板块-功能`` names Jira actually uses.

        229 of CargoWare's components are written ``海运出口-海运订舱``: the ticket
        names the section *and* the screen. No route carries that whole string,
        but both halves exist, so the name is split and the halves are required
        to agree — the leaf must sit inside the section's own area, otherwise a
        coincidental same-named menu in an unrelated app would be accepted.

        "Its own area" means the router module when the repo splits one file per
        module (Vue 2), and the route path subtree otherwise (Vue 3). Judging by
        the parent route's component does not work: a section's component is
        ``Layout``, so every leaf looks like an outsider.
        """

        name = (title or "").strip()
        entry, rows = self.resolve(name, prefer=prefer)
        if entry is not None:
            return Match(entry, tuple(rows), "exact")
        if rows:
            return Match(None, tuple(rows), "ambiguous")

        parts = [part for part in _COMPOUND_SPLIT.split(name) if len(part) >= 2]
        if len(parts) < 2:
            return Match()
        section, _ = self.resolve(parts[0], prefer=prefer)
        if section is None:
            return Match()

        leaf_rows = self.lookup(parts[-1])
        same_area = (
            [row for row in leaf_rows if row.scope == section.scope]
            if not self._scope_is_app and section.scope
            else []
        )
        if len(same_area) == 1:
            return Match(same_area[0], tuple(same_area), "compound")
        if len(same_area) > 1:
            return Match(None, tuple(same_area), "ambiguous")

        leaf, _ = self.resolve(parts[-1], prefer=prefer)
        if leaf is None:
            return Match()
        home = section.file.rsplit("/", 1)[0] + "/"
        inside = (
            leaf.file == section.file
            or leaf.path.startswith(section.path.rstrip("/") + "/")
            or (leaf.app == section.app and leaf.file.startswith(home))
        )
        if not inside:
            return Match()
        return Match(leaf, (leaf,), "compound")
