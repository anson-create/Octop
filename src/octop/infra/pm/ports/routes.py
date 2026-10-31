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
    # True when other routes live under this path: the title names a section,
    # and the component is only that section's default child.
    section: bool = False
    # Bytes on disk. An entry under ~1KB is an entrance shell that imports the
    # real page one level down (``order/index.vue`` -> ``OrderListUnified.vue``).
    # Harmless for rendering, but it tells a reviewer they are looking at a
    # wrapper rather than the screen itself.
    size: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "path": self.path,
            "app": self.app,
            "component": self.component,
            "file": self.file,
            "section": self.section,
            "size": self.size,
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
    router = app_src / "router"
    if not router.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(router.glob("*.ts")):
        text = str(path)
        if _SKIP_NAME_RE.search(path.name) or any(marker in text for marker in _SKIP_DIRS):
            continue
        out.append(path)
    return out


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


def _parse_route_file(path: Path, *, app: str, app_src: Path) -> list[RouteEntry]:
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
        target = app_src / relative
        rows.append(
            RouteEntry(
                title=title.group(1).strip(),
                path=route_path.group(1) if route_path else "",
                app=app,
                component=spec,
                file=relative,
                route_file=str(path),
                size=target.stat().st_size if target.is_file() else 0,
            )
        )
    return rows


class RouteIndex:
    """Routes across every sub-app of a front-end monorepo, keyed by title."""
    def __init__(self, entries: list[RouteEntry]) -> None:
        self._entries = entries
        self._by_title: dict[str, list[RouteEntry]] = {}
        for entry in entries:
            self._by_title.setdefault(entry.title, []).append(entry)
        self._mark_sections()

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
                    section=nested,
                    size=entry.size,
                )
            )
        self._entries = marked
        self._by_title = {}
        for entry in marked:
            self._by_title.setdefault(entry.title, []).append(entry)

    @classmethod
    def load(cls, repo_root: Path, *, apps: tuple[str, ...] | None = None) -> RouteIndex | None:
        """Parse every sub-app router. ``None`` when the repo has no router at all."""

        root = Path(repo_root)
        if not root.is_dir():
            return None
        targets = apps or tuple(
            child.name
            for child in sorted(root.iterdir())
            if child.is_dir() and (child / "src" / "router").is_dir()
        )
        entries: list[RouteEntry] = []
        for app in targets:
            app_src = root / app / "src"
            for route_file in _route_files(app_src):
                entries.extend(_parse_route_file(route_file, app=app, app_src=app_src))
        if not entries:
            return None
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def titles(self) -> set[str]:
        return set(self._by_title)

    def lookup(self, title: str) -> list[RouteEntry]:
        """Every route whose title is exactly this. Empty list means no match."""

        return list(self._by_title.get((title or "").strip(), ()))

    def resolve(self, title: str) -> tuple[RouteEntry | None, list[RouteEntry]]:
        """``(the_one, candidates)`` — the_one is None unless it is unambiguous.

        Unambiguous means exactly one route carries the title. Two routes with
        the same title in two apps are two different pages, and choosing either
        would be a confident guess.
        """

        rows = self.lookup(title)
        return (rows[0] if len(rows) == 1 else None), rows

    def match(self, title: str) -> Match:
        """Locate a component, including the ``板块-功能`` names Jira actually uses.

        229 of CargoWare's components are written ``海运出口-海运订舱``: the ticket
        names the section *and* the screen. No route carries that whole string,
        but both halves exist, so the name is split and the halves are required
        to agree — the leaf must sit inside the section's own subtree, otherwise
        a coincidental same-named menu in an unrelated app would be accepted.
        """

        name = (title or "").strip()
        rows = self.lookup(name)
        if len(rows) == 1:
            return Match(rows[0], tuple(rows), "exact")
        if rows:
            return Match(None, tuple(rows), "ambiguous")

        parts = [part for part in _COMPOUND_SPLIT.split(name) if len(part) >= 2]
        if len(parts) < 2:
            return Match()
        section, _ = self.resolve(parts[0])
        leaf, _ = self.resolve(parts[-1])
        if section is None or leaf is None:
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
