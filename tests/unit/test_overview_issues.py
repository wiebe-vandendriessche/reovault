"""The Overview lists every open condition, most urgent first. The bug this
guards: a single fleet banner picked "bad" over "warn", so failed clips on
one camera hid another camera's clips that were about to be overwritten."""

from __future__ import annotations

from datetime import timedelta

from reovault.health import CardStatus
from reovault.web.api.health import _banner, _issues


def _card(
    state: str = "all_archived", safe_for: timedelta | None = None, lost: int = 0
) -> CardStatus:
    return CardStatus(
        state=state,  # type: ignore[arg-type]
        waiting_count=4 if state == "at_risk" else 0,
        lost_count=lost,
        oldest_waiting_utc=None,
        oldest_on_card_utc=None,
        window=None,
        window_estimated=False,
        safe_for=safe_for,
        used_fraction=None,
    )


def _order(*cameras: tuple[CardStatus, bool, int]) -> list[str]:
    ranked = [(r, s, b.text) for card, down, n in cameras for r, s, b in _issues(card, down, n)]
    return [text for _, _, text in sorted(ranked)]


def test_clips_at_risk_outrank_failed_clips_on_another_camera() -> None:
    order = _order(
        (_card(), False, 7),
        (_card("at_risk", timedelta(hours=15)), False, 0),
    )
    assert order[0].startswith("4 clips not archived yet")
    assert order[1] == "7 recordings need attention."


def test_the_soonest_overwrite_comes_first() -> None:
    order = _order(
        (_card("at_risk", timedelta(hours=20)), False, 0),
        (_card("at_risk", timedelta(hours=2)), False, 0),
    )
    assert "2 hours" in order[0] and "20 hours" in order[1]


def test_one_camera_can_have_several_issues_and_health_shows_the_worst() -> None:
    card = _card("at_risk", timedelta(hours=3))
    assert len(_issues(card, True, 2)) == 3
    banner = _banner(card, True, 2)
    assert banner is not None and banner.level == "warn" and "overwritten" in banner.text


def test_a_healthy_camera_has_no_issues() -> None:
    assert _issues(_card(), False, 0) == []
    assert _banner(_card(), False, 0) is None


def test_lost_clips_are_a_warning_between_at_risk_and_failed() -> None:
    order = _order(
        (_card(), False, 2),
        (_card(lost=3), False, 0),
        (_card("at_risk", timedelta(hours=5)), False, 0),
    )
    assert order[0].startswith("4 clips not archived yet")
    assert order[1] == "3 clips were overwritten on the camera before they could be archived."
    assert order[2] == "2 recordings need attention."
