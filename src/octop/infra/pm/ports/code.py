"""Code port: turn a symbol into a clickable ``code_span``.

Deliberately read-only and bounded — it walks a checkout, matches a literal
needle, and returns file/line spans. It never writes, and it stops after
``max_files`` so a huge monorepo cannot stall a tick.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from octop.infra.pm.ports.base import PortResult, PortUnavailable

_SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".vue", ".java", ".sql", ".md")
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}


class CodePort:
    name = "code"

    def __init__(self, root: Path | None, *, repo_name: str = "", max_files: int = 5000) -> None:
        self._root = Path(root) if root else None
        self._repo_name = repo_name or (self._root.name if self._root else "")
        self._max_files = max_files

    def available(self) -> bool:
        return self._root is not None and self._root.is_dir()

    def fetch(self, *, query: str, task_ref: str, limit: int = 10) -> PortResult:
        if not self.available():
            return PortResult(port=self.name, unavailable_reason="code root not configured")
        needle = (query or "").strip()
        if not needle:
            raise PortUnavailable("query is empty")

        envelopes: list[dict[str, Any]] = []
        visited = 0
        assert self._root is not None
        for path in self._iter_sources(self._root):
            visited += 1
            if visited > self._max_files:
                break
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle not in text:
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if needle not in line:
                    continue
                start = max(1, index)
                end = min(len(lines), index + 3)
                excerpt = "\n".join(lines[start - 1 : end]).strip()
                envelopes.append(
                    {
                        "source_type": "code",
                        "port": "code",
                        "locator": {
                            "kind": "code_span",
                            "repo": self._repo_name,
                            "path": str(path.relative_to(self._root)),
                            "line": [start, end],
                            "symbol": needle,
                        },
                        "excerpt": excerpt,
                        "visibility": "internal",
                        "confidence": 0.8,
                    }
                )
                if len(envelopes) >= limit:
                    return PortResult(port=self.name, envelopes=envelopes)
        return PortResult(port=self.name, envelopes=envelopes)

    def _iter_sources(self, root: Path) -> Iterator[Path]:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                        continue
                    stack.append(entry)
                elif entry.suffix in _SOURCE_SUFFIXES:
                    yield entry
