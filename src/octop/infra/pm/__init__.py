"""PM 需求方案工作台 — a domain package inside the Octop kernel.

Layout: ``domain`` (rules) → ``repos`` (SQL) → ``service`` (application) →
``api`` (HTTP). ``migrate`` keeps its own version table so upstream releases
never collide with PM schema numbers.
"""

from __future__ import annotations

__all__ = ["WORKFLOW_KEY"]

from octop.infra.pm.domain.stages import WORKFLOW_KEY
