"""PM repos, bundled for the service layer."""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.repos.artifact import ArtifactRepo
from octop.infra.pm.repos.conclusion import ConclusionInput, ConclusionRepo
from octop.infra.pm.repos.decision import DecisionRepo
from octop.infra.pm.repos.evidence import EvidenceRepo
from octop.infra.pm.repos.integration import IntegrationRepo
from octop.infra.pm.repos.metrics import EvalCaseRepo, MetricRepo
from octop.infra.pm.repos.requirement import RequirementRepo
from octop.infra.pm.repos.review import PERSPECTIVES, ReviewRepo
from octop.infra.pm.repos.step import HandlerRepo, StepRepo
from octop.infra.pm.repos.task import TaskRepo
from octop.infra.pm.repos.workspace import WorkspaceRepo


class PmRepos:
    """One place to construct every repo against a pool."""

    def __init__(self, db: DatabasePool) -> None:
        self.db = db
        self.workspaces = WorkspaceRepo(db)
        self.requirements = RequirementRepo(db)
        self.tasks = TaskRepo(db)
        self.steps = StepRepo(db)
        self.handlers = HandlerRepo(db)
        self.artifacts = ArtifactRepo(db)
        self.conclusions = ConclusionRepo(db)
        self.evidence = EvidenceRepo(db)
        self.decisions = DecisionRepo(db)
        self.reviews = ReviewRepo(db)
        self.integrations = IntegrationRepo(db)
        self.metrics = MetricRepo(db)
        self.eval_cases = EvalCaseRepo(db)


__all__ = [
    "ArtifactRepo",
    "ConclusionInput",
    "ConclusionRepo",
    "DecisionRepo",
    "EvalCaseRepo",
    "EvidenceRepo",
    "HandlerRepo",
    "IntegrationRepo",
    "MetricRepo",
    "PERSPECTIVES",
    "PmRepos",
    "RequirementRepo",
    "ReviewRepo",
    "StepRepo",
    "TaskRepo",
    "WorkspaceRepo",
]
