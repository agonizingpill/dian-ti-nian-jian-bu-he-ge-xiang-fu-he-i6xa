"""Pure ranking rules for the daily supervisor spot-check quota.

The quota is filled from the tree's triage conclusions (``analysis_jobs``) that
already produced a result.  Rules implemented here, free of any database
dependency so they can be unit-tested in isolation:

* one elevator per day -> only its earliest accepted conclusion competes;
* an elevator that already owns a claimed seat never occupies a second seat;
* leaf nodes not yet covered by a claimed spot check are filled first;
* equal priority falls back to acceptance order (then record id);
* every record outside the allocation has lost and can never be claimed;
* claimed seats are never re-ranked, so late conclusions only reshuffle open seats.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _order(candidate: Mapping[str, Any]):
    """Acceptance order: earliest accepted first, then lowest record id."""
    return (candidate.get("accepted_at"), candidate.get("job_id"))


def _uncovered(candidate: Mapping[str, Any], coverage: set[str]) -> bool:
    """A record counts as uncovered unless its known leaf is already covered."""
    key = candidate.get("leaf_key")
    if not candidate.get("leaf_known", True) or not key:
        return True
    return key not in coverage


def rank(
    candidates: Iterable[Mapping[str, Any]],
    claimed: Iterable[Mapping[str, Any]],
    covered_leaves: Iterable[str],
    open_seats: int,
):
    """Rank one day's records into claimable / lost / superseded.

    ``candidates`` are every completed triage record accepted on the day.
    ``claimed`` are the day's frozen seats (needs ``job_id`` and ``lift_id``).
    ``covered_leaves`` are leaf keys already sampled by any claimed spot check.
    ``open_seats`` is how many seats are still available.
    """
    open_seats = max(0, int(open_seats))
    claimed = list(claimed)
    claimed_job_ids = {c["job_id"] for c in claimed}
    claimed_lifts = {c["lift_id"] for c in claimed}

    candidates = list(candidates)

    # "同台梯取当天最早接单的已出结论复核": collapse each elevator to its
    # earliest accepted record, so a later record can never represent a lift
    # whose earlier conclusion arrived late.
    earliest: dict[Any, Mapping[str, Any]] = {}
    for candidate in candidates:
        current = earliest.get(candidate["lift_id"])
        if current is None or _order(candidate) < _order(current):
            earliest[candidate["lift_id"]] = candidate
    earliest_ids = {candidate["job_id"] for candidate in earliest.values()}

    superseded = sorted(
        (c for c in candidates if c["job_id"] not in earliest_ids and c["job_id"] not in claimed_job_ids),
        key=_order,
    )

    blocked = sorted(
        (c for c in earliest.values()
         if c["job_id"] not in claimed_job_ids and c["lift_id"] in claimed_lifts),
        key=_order,
    )
    pool = [
        candidate
        for candidate in earliest.values()
        if candidate["job_id"] not in claimed_job_ids and candidate["lift_id"] not in claimed_lifts
    ]

    coverage = set(covered_leaves)
    selected: list[Mapping[str, Any]] = []
    remaining = list(pool)
    while remaining and len(selected) < open_seats:
        # "优先补没覆盖的叶节点，同等时按接单先后"
        best = min(remaining, key=lambda c: (0 if _uncovered(c, coverage) else 1, *_order(c)))
        remaining.remove(best)
        selected.append(best)
        if best.get("leaf_known", True) and best.get("leaf_key"):
            coverage.add(best["leaf_key"])

    lost = sorted(remaining, key=_order)
    return {
        "selected": selected,
        "lost": lost,
        "blocked": blocked,
        "superseded": superseded,
        "coverage_after": coverage,
    }
