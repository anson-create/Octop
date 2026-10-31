"""The sidebar badge counts what is actually waiting on a human.

A badge that over-counts is worse than no badge: it trains people to ignore it.
So these tests pin the three things that decide the number — which statuses count,
whether unassigned cards count for everyone, and whether overdue is reported
separately from just-pending.
"""

from __future__ import annotations

from octop.infra.pm.repos._base import iso_in


def _card(service, task_id: int, *, level: str, minutes: int, assignee: int | None = None):
    return service.repos.decisions.create(
        task_id=task_id,
        level=level,
        question=f"{level} 待拍板",
        options=[{"key": "yes", "label": "是"}, {"key": "no", "label": "否"}],
        recommended_key="yes",
        timeout_at=iso_in(minutes),
        assignee_user_id=assignee,
    )


def test_inbox_counts_pending_by_level(service, task) -> None:
    _card(service, task["id"], level="L2", minutes=30)
    _card(service, task["id"], level="L2", minutes=45)
    _card(service, task["id"], level="L3", minutes=600)

    inbox = service.inbox()

    assert inbox["pending"] == 3
    assert inbox["overdue"] == 0
    assert inbox["by_level"] == {"L2": 2, "L3": 1}
    assert inbox["next_timeout_at"]


def test_resolved_cards_leave_the_inbox(service, task) -> None:
    card = _card(service, task["id"], level="L2", minutes=30)
    assert service.inbox()["pending"] == 1

    service.repos.decisions.expire(card.id)

    assert service.inbox()["pending"] == 0


def test_overdue_is_counted_separately_from_pending(service, task) -> None:
    """An overdue card is still pending — but it is the one that needs shouting about."""
    _card(service, task["id"], level="L2", minutes=-5)
    _card(service, task["id"], level="L2", minutes=30)

    inbox = service.inbox()

    assert inbox["pending"] == 2
    assert inbox["overdue"] == 1


def test_unassigned_cards_are_everyones(service, task) -> None:
    """A card nobody owns is the one that expires unseen, so it must not be hidden."""
    _card(service, task["id"], level="L2", minutes=30, assignee=None)
    _card(service, task["id"], level="L2", minutes=30, assignee=7)

    assert service.inbox(user_id=7)["pending"] == 2
    assert service.inbox(user_id=8)["pending"] == 1


def test_inbox_is_empty_on_a_fresh_workspace(service) -> None:
    inbox = service.inbox()
    assert inbox["pending"] == 0
    assert inbox["overdue"] == 0
    assert inbox["by_level"] == {}
