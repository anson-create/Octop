"""Which repository a requirement lands in — decided by the Jira project key.

A prototype is only 1:1 with a product if it is derived from that product's own
source. That makes "which repo?" a correctness question, not a routing detail:
deriving a CargoWareX page from CargoWare's Vue 2 + Element UI code produces
markup that is wrong in every way that matters — component library, tokens,
class names, styling strategy.

Two systems, two layers each:

    cargoware    web  Vue 2.6 + Element UI 2.15      api  Java (Maven, 18 modules)
    cargowarex   web  Vue 3.4 + Arco 2.57 + Tailwind api  Java (Maven)

The mapping runs **Jira project key -> repository**, never a keyword search over
every checkout. A keyword search would sooner or later read the wrong product's
source and then produce a confident, structurally faithful, completely wrong
prototype — the worst possible failure, because it looks right.

``DEFAULT_PROJECT_SYSTEM`` only seeds the two projects we know. Project keys are
chosen per Jira instance, so the authoritative map is the one saved against the
integration (``pm_integration.mapping['projects']``) and edited in the UI from the
real project list Jira reports. Anything unmapped resolves to ``None`` and the
caller must ask — never guess.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Jira project key -> product system. Seed values only; a saved mapping wins.
DEFAULT_PROJECT_SYSTEM: dict[str, str] = {
    "CCTOWER": "cargowarex",
    "CARGO": "cargoware",
}

SYSTEMS = ("cargoware", "cargowarex")

# Framework families decide how the prototype is rendered. Vue 2 and Vue 3 need
# different SSR packages and have different component resolution rules, so this
# is not cosmetic metadata.
FRAMEWORK_VUE3 = "vue3"
FRAMEWORK_VUE2 = "vue2"
FRAMEWORK_JAVA = "java"

# Renderer asset names inside ``runtime/assets/``. These are filenames shipped in
# the wheel, not deployment paths — the deployment path is decided at run time.
RENDERER_VUE3 = "render.mjs"
RENDERER_VUE2 = "render.vue2.mjs"


@dataclass(frozen=True)
class SsrPlan:
    """How to render this repository, and what has to exist before it can.

    Split out because the two Vue generations differ in *where* their toolchain
    lives, and getting that wrong is not a cosmetic mistake:

    * **Vue 3** is built with Vite. The renderer boots the repo's own Vite config
      and resolves ``vite`` from the mirror, so the only prerequisite is that the
      mirror has been ``npm ci``-ed.
    * **Vue 2** (CargoWare) is built with webpack 4. It has no ``vite`` and no
      ``vue-server-renderer`` — those two plus a Vue 2 SFC plugin are the
      renderer's own toolchain. Installing them into the product's tree would
      both pollute a source tree that is re-synced by script and be wiped by the
      next sync, so they live in a service-owned directory beside the mirrors.

    ``mirror_requirements`` are package names resolved from the mirror's
    ``node_modules``; ``toolchain_requirements`` from the toolchain directory's.
    """

    script: str
    mirror_requirements: tuple[str, ...] = ()
    toolchain: str = ""
    toolchain_requirements: tuple[str, ...] = ()


# Empty ``script`` means "this repository cannot be rendered at all" — an api
# layer, and anything whose framework is not a browser UI.
_SSR_PLANS: dict[str, SsrPlan] = {
    FRAMEWORK_VUE3: SsrPlan(
        script=RENDERER_VUE3,
        # `vite` is a devDependency of every Vite app: an install done with
        # --omit=dev produces a mirror that renders nothing, so it is checked.
        mirror_requirements=("vue", "vue-router", "vite"),
    ),
    FRAMEWORK_VUE2: SsrPlan(
        script=RENDERER_VUE2,
        mirror_requirements=(
            "vue",
            "vue-template-compiler",
            "element-ui",
            "vue-router",
            "vuex",
            "vue-i18n",
        ),
        # Pinned by the renderer to match the mirror: vue-server-renderer and
        # vue-template-compiler must be the same major.minor as the app's vue,
        # otherwise Vue refuses to render at all. The compiler is listed
        # explicitly because npm's automatic peer install would pick the latest
        # 2.x (2.7) and mismatch this app's 2.6.
        toolchain="vue2",
        toolchain_requirements=(
            "vite",
            "vite-plugin-vue2",
            "vue-server-renderer",
            "vue-template-compiler",
        ),
    ),
}


@dataclass(frozen=True)
class RepoSpec:
    """One checkout of one layer of one product."""

    key: str
    system: str
    layer: str  # web | api
    framework: str
    ui_lib: str = ""
    # Where the read-only mirror lives on the host running the PM service.
    mirror: str = ""
    # Recorded at sync time; empty means "unknown, must be fingerprinted".
    git_ref: str = ""
    # Which sub-app a requirement belongs to when the mirror is a monorepo. A
    # CargoWareX ticket is a *tenant* ticket: 系统设置 exists in both the tenant
    # app and the platform-admin back office, and they are different pages for
    # different audiences. Empty means the repo has one app and no preference is
    # needed; it never overrides a genuinely ambiguous match inside that app.
    preferred_app: str = ""

    @property
    def is_web(self) -> bool:
        return self.layer == "web"

    @property
    def renderable(self) -> bool:
        """Only a Vue front-end can be rendered into a prototype."""

        return self.framework in (FRAMEWORK_VUE3, FRAMEWORK_VUE2)

    @property
    def ssr(self) -> SsrPlan:
        """The renderer for this framework, or a plan with no script.

        Callers must treat an empty ``script`` as "cannot render", never as
        "use the default one" — using the Vue 3 renderer on a Vue 2 page is how
        a prototype ends up structurally faithful to the wrong product.
        """

        return _SSR_PLANS.get(self.framework, SsrPlan(script=""))

    def root(self) -> Path:
        return Path(self.mirror) if self.mirror else Path("/nonexistent")


# Mirror layout is deliberately flat and predictable: one directory per spec key.
# Anything else (a developer's checkout, an iCloud copy) is a local override, not
# the baseline the service reads.
_DEFAULT_MIRROR_ROOT = "/opt/pm-mirror"

REPOS: dict[str, RepoSpec] = {
    "cwx-web": RepoSpec(
        key="cwx-web",
        system="cargowarex",
        layer="web",
        framework=FRAMEWORK_VUE3,
        ui_lib="arco",
        mirror=f"{_DEFAULT_MIRROR_ROOT}/cwx-platform-admin",
        preferred_app="control-tower",
    ),
    "cwx-api": RepoSpec(
        key="cwx-api",
        system="cargowarex",
        layer="api",
        framework=FRAMEWORK_JAVA,
        mirror=f"{_DEFAULT_MIRROR_ROOT}/cwx-api",
    ),
    "cw-web": RepoSpec(
        key="cw-web",
        system="cargoware",
        layer="web",
        framework=FRAMEWORK_VUE2,
        ui_lib="element-ui",
        mirror=f"{_DEFAULT_MIRROR_ROOT}/cw-web",
    ),
    "cw-api": RepoSpec(
        key="cw-api",
        system="cargoware",
        layer="api",
        framework=FRAMEWORK_JAVA,
        mirror=f"{_DEFAULT_MIRROR_ROOT}/cw-api",
    ),
}


def project_key_of(issue_key: str) -> str:
    """``CCTOWER-3597`` -> ``CCTOWER``.

    Jira's own ``fields.project.key`` is preferred where available; this is the
    fallback for rows that only stored the issue key (everything created before
    the project was recorded in the baseline).
    """

    return (issue_key or "").strip().split("-", 1)[0].strip().upper()


@dataclass(frozen=True)
class ProjectBinding:
    """The resolved answer to "which source does this requirement belong to"."""

    project: str
    system: str
    repos: tuple[str, ...]
    origin: str  # "mapping" (saved and explicit) | "default" (seed table)

    def spec(self, layer: str) -> RepoSpec | None:
        for key in self.repos:
            spec = REPOS.get(key)
            if spec is not None and spec.layer == layer:
                return spec
        return None

    @property
    def web(self) -> RepoSpec | None:
        return self.spec("web")

    @property
    def api(self) -> RepoSpec | None:
        return self.spec("api")

    def describe(self) -> str:
        shown = "、".join(self.repos) if self.repos else "未映射"
        return f"{self.project} → {self.system}（{shown}，来源：{'已保存映射' if self.origin == 'mapping' else '默认表'})"


def _parse_target(project: str, value: str) -> ProjectBinding | None:
    """Accept a system name, a repo key, or a comma-separated repo key list."""

    raw = (value or "").strip()
    if not raw:
        return None
    keys: list[str] = []
    for piece in raw.split(","):
        token = piece.strip()
        if not token:
            continue
        if token in SYSTEMS:
            # A system means "every layer of it", which keeps the mapping usable
            # before the api mirror is even synced.
            keys.extend(spec.key for spec in repos_for_system(token))
        elif token in REPOS:
            keys.append(REPOS[token].key)
        else:
            return None
    if not keys:
        return None
    # Deduplicate while keeping declaration order: web before api matters to the
    # caller that only wants a page to render.
    ordered = tuple(dict.fromkeys(keys))
    system = REPOS[ordered[0]].system
    if any(REPOS[key].system != system for key in ordered):
        # A project cannot straddle both products; that mapping is a typo.
        return None
    return ProjectBinding(project=project, system=system, repos=ordered, origin="mapping")


def resolve_project(
    project: str,
    overrides: Mapping[str, str] | None = None,
) -> ProjectBinding | None:
    """Resolve one Jira project key to its repositories.

    ``overrides`` is the saved mapping from the integration row. It is consulted
    first because it reflects what the operator actually configured; the seed
    table is only a convenience for the two projects we ship knowing about.
    """

    key = (project or "").strip().upper()
    if not key:
        return None
    if overrides:
        saved = overrides.get(key)
        if isinstance(saved, str):
            binding = _parse_target(key, saved)
            if binding is not None:
                return binding
    system = DEFAULT_PROJECT_SYSTEM.get(key)
    if system is None:
        return None
    return ProjectBinding(
        project=key,
        system=system,
        repos=tuple(spec.key for spec in repos_for_system(system)),
        origin="default",
    )


def resolve_issue(issue_key: str, overrides: Mapping[str, str] | None = None) -> ProjectBinding | None:
    return resolve_project(project_key_of(issue_key), overrides)


def project_of_requirement(
    baseline: Mapping[str, Any] | None,
    source_ref: str,
    source: str = "",
) -> str:
    """The project a stored requirement belongs to.

    Recorded at intake as ``baseline.jira_project``; falls back to the issue key
    prefix only for Jira-sourced rows written before that field existed. A
    non-Jira ``source_ref`` (``acceptance/2026-10-31``, a spreadsheet id) would
    otherwise be parsed into a project that never existed. Never inferred from
    the requirement's title or text — that would be a keyword guess.
    """

    recorded = str((baseline or {}).get("jira_project") or "").strip().upper()
    if recorded:
        return recorded
    if source and source.strip().lower() != "jira":
        return ""
    return project_key_of(source_ref)


def web_repo_for_project(
    project: str,
    overrides: Mapping[str, str] | None = None,
) -> RepoSpec | None:
    """The one repository a prototype can be derived from, or ``None``."""

    binding = resolve_project(project, overrides)
    return binding.web if binding is not None else None


def project_keys(overrides: Mapping[str, str] | None = None) -> list[str]:
    """Every project the service can route today, saved mappings first."""

    keys = {key.upper() for key in (overrides or {})}
    keys.update(DEFAULT_PROJECT_SYSTEM)
    return sorted(keys)


def repos_for_system(system: str) -> list[RepoSpec]:
    return [spec for spec in REPOS.values() if spec.system == system]
