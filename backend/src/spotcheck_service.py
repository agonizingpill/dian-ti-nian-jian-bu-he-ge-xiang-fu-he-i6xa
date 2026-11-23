"""Daily supervisor spot-check quota over the tree's triage conclusions.

A supervisor reserves a few seats each day to spot-check the decision tree's
triage suggestions.  A seat (名额) belongs to the acceptance day of the
``analysis_jobs`` record that produced the conclusion, and to that elevator.

Rules enforced here:

* only completed triage records (``status='completed'`` with a prediction)
  compete; a record is attributed to the day it was accepted
  (``analysis_jobs.created_at``) even if the worker finishes it late;
* for one elevator only its earliest accepted conclusion of that day competes;
* an elevator that already owns a claimed seat never occupies a second seat;
* leaf nodes not yet covered by any claimed spot check are filled first, ties
  broken by acceptance order;
* claimed seats are frozen - a late record only re-ranks the still-open seats;
* records outside the allocation have lost and can never be claimed;
* claiming a seat you already own is idempotent and does not consume another
  seat; concurrent claims are serialized per day, so only the first succeeds
  and the other supervisor reads the updated roster.
"""

from __future__ import annotations

import json
import os
from datetime import date as date_cls
from threading import Lock

import pymysql

from .db import DB, connection
from .spotcheck_ranking import rank

DEFAULT_DAILY_QUOTA = int(os.getenv("SPOTCHECK_DAILY_QUOTA", "3"))

_schema_lock = Lock()
_schema_ready = False

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS spotcheck_quota_day (
        quota_date DATE NOT NULL PRIMARY KEY,
        daily_limit INT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS spotcheck_seats (
        quota_date DATE NOT NULL,
        job_id INT NOT NULL,
        lift_id INT NOT NULL,
        leaf_id INT NULL,
        leaf_key VARCHAR(120) NULL,
        claimed_by INT NOT NULL,
        claimed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (quota_date, job_id),
        UNIQUE KEY uq_spotcheck_day_lift (quota_date, lift_id),
        KEY idx_spotcheck_job (job_id),
        KEY idx_spotcheck_lift (lift_id),
        KEY idx_spotcheck_claimant (claimed_by),
        CONSTRAINT fk_spotcheck_job FOREIGN KEY (job_id) REFERENCES analysis_jobs(id) ON DELETE CASCADE,
        CONSTRAINT fk_spotcheck_lift FOREIGN KEY (lift_id) REFERENCES lifts(id) ON DELETE CASCADE,
        CONSTRAINT fk_spotcheck_claimant FOREIGN KEY (claimed_by) REFERENCES users(id)
    )
    """,
]

_JOB_SQL = """
    SELECT j.id AS job_id, j.lift_id, j.prediction, j.probability,
           j.path_rules_json, j.created_at AS accepted_at, j.completed_at,
           l.asset_code, l.building
    FROM analysis_jobs j JOIN lifts l ON l.id = j.lift_id
    WHERE j.status = 'completed' AND j.prediction IS NOT NULL
      AND DATE(j.created_at) = %s
"""


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with connection() as conn:
            with conn.cursor() as cur:
                for statement in _SCHEMA:
                    cur.execute(statement)
        _schema_ready = True


def today():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_DATE AS d")
            return cur.fetchone()["d"]


def resolve_day(value):
    if value is None:
        return today()
    if isinstance(value, date_cls):
        return value
    return date_cls.fromisoformat(str(value))


def ensure_day(cur, day, lock=False):
    cur.execute("SELECT daily_limit FROM spotcheck_quota_day WHERE quota_date=%s" + (" FOR UPDATE" if lock else ""),
                (day,))
    row = cur.fetchone()
    return int(row["daily_limit"]) if row else None


def ensure_day_row(day):
    """Create the day row in its own autocommit statement.

    Doing this outside the claim transaction avoids the insert-intention
    deadlock that concurrent ``INSERT IGNORE`` on the same primary key causes,
    and guarantees the later ``SELECT ... FOR UPDATE`` only takes a record lock.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO spotcheck_quota_day(quota_date,daily_limit) VALUES(%s,%s)",
                        (day, max(0, DEFAULT_DAILY_QUOTA)))


def path_payload(raw):
    if not raw:
        return {}
    payload = raw
    if not isinstance(payload, dict):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}


def stamp(value):
    return value.isoformat(sep=" ") if hasattr(value, "isoformat") else value


def job_view(row):
    payload = path_payload(row["path_rules_json"])
    leaf_id = payload.get("leaf_id")
    leaf_key = payload.get("leaf_key") or (f"leaf-{leaf_id}" if leaf_id is not None else None)
    branch = payload.get("branch") or []
    if isinstance(branch, str):
        branch = [branch]
    return {
        "job_id": int(row["job_id"]),
        "lift_id": int(row["lift_id"]),
        "asset_code": row["asset_code"],
        "building": row["building"],
        "prediction": row["prediction"],
        "probability": float(row["probability"]) if row["probability"] is not None else None,
        "accepted_at": stamp(row["accepted_at"]),
        "completed_at": stamp(row["completed_at"]),
        "leaf_id": leaf_id,
        "leaf_key": leaf_key,
        "leaf_known": leaf_key is not None,
        "branch": [str(step) for step in branch],
        "branch_text": payload.get("branch_text") or " → ".join([*map(str, branch), str(row["prediction"])]),
    }


def seat_view(row):
    return {
        "job_id": int(row["job_id"]),
        "lift_id": int(row["lift_id"]),
        "leaf_id": row["leaf_id"],
        "leaf_key": row["leaf_key"],
        "claimed_by": int(row["claimed_by"]),
        "claimed_by_name": row.get("claimed_by_name") or str(row["claimed_by"]),
        "claimed_at": stamp(row["claimed_at"]),
    }


def load_seats(cur, day, lock=False):
    cur.execute("""
        SELECT s.job_id, s.lift_id, s.leaf_id, s.leaf_key, s.claimed_by, s.claimed_at,
               u.name AS claimed_by_name
        FROM spotcheck_seats s LEFT JOIN users u ON u.id = s.claimed_by
        WHERE s.quota_date = %s ORDER BY s.claimed_at, s.job_id
    """ + (" FOR UPDATE" if lock else ""), (day,))
    return [seat_view(row) for row in cur.fetchall()]


def covered_leaves(cur):
    """Leaves already sampled by any claimed seat (for ranking priority)."""
    cur.execute("SELECT DISTINCT leaf_key FROM spotcheck_seats WHERE leaf_key IS NOT NULL")
    return {row["leaf_key"] for row in cur.fetchall()}


def build_snapshot(day, limit, jobs, seats, covered):
    views = {job["job_id"]: job for job in jobs}
    earliest_for_lift = {}
    for job in jobs:
        current = earliest_for_lift.get(job["lift_id"])
        if current is None or (job["accepted_at"], job["job_id"]) < (current["accepted_at"], current["job_id"]):
            earliest_for_lift[job["lift_id"]] = job

    outcome = rank(jobs, seats, covered, max(0, limit - len(seats)))

    def entry(job, status, reason=None, position=None):
        item = {**job, "seat_status": status, "claim": None,
                "earliest_for_lift": earliest_for_lift.get(job["lift_id"], {}).get("job_id") == job["job_id"]}
        if reason:
            item["seat_reason"] = reason
        if position is not None:
            item["rank"] = position
        return item

    claimed = []
    for seat in seats:
        view = views.get(seat["job_id"]) or fallback(seat)
        claimed.append({**entry(view, "claimed"),
                        "claim": {"by": seat["claimed_by"], "by_name": seat["claimed_by_name"],
                                  "at": seat["claimed_at"]}})

    claimable = [entry(job, "claimable", position=index + 1)
                 for index, job in enumerate(outcome["selected"])]
    lost = ([entry(job, "lost", reason="quota_exhausted") for job in outcome["lost"]]
            + [entry(job, "lost", reason="later_record_same_lift") for job in outcome["superseded"]]
            + [entry(job, "lost", reason="lift_already_claimed") for job in outcome["blocked"]])

    return {
        "date": day.isoformat(),
        "daily_limit": limit,
        "claimed_count": len(seats),
        "open_seats": max(0, limit - len(seats)),
        "claimable": claimable,
        "claimed": claimed,
        "lost": lost,
        "covered_leaves": sorted(leaf for leaf in covered if leaf),
    }


def fallback(seat):
    return {"job_id": seat["job_id"], "lift_id": seat["lift_id"], "asset_code": None, "building": None,
            "prediction": None, "probability": None, "accepted_at": None, "completed_at": None,
            "leaf_id": seat.get("leaf_id"), "leaf_key": seat.get("leaf_key"),
            "leaf_known": seat.get("leaf_key") is not None, "branch": [], "branch_text": None}


def allocation(day=None):
    """Read-only allocation snapshot for the workbench."""
    ensure_schema()
    day = resolve_day(day)
    ensure_day_row(day)
    with connection() as conn:
        with conn.cursor() as cur:
            limit = ensure_day(cur, day)
            cur.execute(_JOB_SQL, (day,))
            jobs = [job_view(row) for row in cur.fetchall()]
            seats = load_seats(cur, day)
            covered = covered_leaves(cur)
    return build_snapshot(day, limit, jobs, seats, covered)


def _transaction_connection():
    config = {key: value for key, value in DB.items() if key != "autocommit"}
    conn = pymysql.connect(**config)
    # READ COMMITTED avoids gap locks on the seats range scan, keeping claims
    # that serialize on the day row free of secondary deadlocks.
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    conn.autocommit(False)
    return conn


def decide_outcome(limit, seats, jobs, job_id, user_id, covered):
    """Pure claim decision used inside the transaction; returns ``(outcome, seat)``."""
    seat_by_job = {seat["job_id"]: seat for seat in seats}
    if job_id in seat_by_job:
        seat = seat_by_job[job_id]
        return ("already" if seat["claimed_by"] == user_id else "taken"), seat
    job = next((item for item in jobs if item["job_id"] == job_id), None)
    if job is None:
        return "unavailable", None
    existing_lift = next((seat for seat in seats if seat["lift_id"] == job["lift_id"]), None)
    if existing_lift is not None:
        return "lift_taken", existing_lift
    outcome = rank(jobs, seats, covered, max(0, limit - len(seats)))
    if job_id not in {item["job_id"] for item in outcome["selected"]}:
        return "lost", None
    return "created", job


def claim_transaction(day, job_id, user_id):
    conn = _transaction_connection()
    try:
        with conn.cursor() as cur:
            # Lock the day row first so concurrent claims on the same day
            # serialize and the loser reads the winning roster.  The row was
            # created beforehand, so this is a plain record lock.
            limit = ensure_day(cur, day, lock=True)
            if limit is None:
                conn.rollback()
                return "retry", None
            seats = load_seats(cur, day)
            cur.execute(_JOB_SQL, (day,))
            jobs = [job_view(row) for row in cur.fetchall()]
            covered = covered_leaves(cur)
            outcome, seat = decide_outcome(limit, seats, jobs, job_id, user_id, covered)
            if outcome == "created":
                cur.execute("""
                    INSERT INTO spotcheck_seats(quota_date,job_id,lift_id,leaf_id,leaf_key,claimed_by)
                    VALUES(%s,%s,%s,%s,%s,%s)
                """, (day, job_id, seat["lift_id"], seat["leaf_id"], seat["leaf_key"], user_id))
        conn.commit()
        return outcome, seat
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        # A duplicate seat / duplicate lift for the day means a concurrent
        # claim won the race; anything else (e.g. a stale claimant token whose
        # user row no longer exists) must not be reported as a lost seat.
        code = exc.args[0] if exc.args else None
        if code == 1062:  # duplicate entry on a seat unique key
            return _post_conflict(day, job_id, user_id)
        if code == 1452:  # foreign key: claimant no longer a valid user
            return "claimant_invalid", None
        raise
    except pymysql.err.OperationalError as exc:
        conn.rollback()
        if exc.args and exc.args[0] in (1205, 1213):  # lock wait timeout, deadlock
            return "retry", None
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _post_conflict(day, job_id, user_id):
    with connection() as conn:
        with conn.cursor() as cur:
            seats = load_seats(cur, day)
    seat = next((seat for seat in seats if seat["job_id"] == job_id), None)
    if seat is not None:
        return ("already" if seat["claimed_by"] == user_id else "taken"), seat
    return "lost", None


def claim(day, job_id, user):
    """Claim one seat; returns ``(outcome, seat_or_job_or_none, day)``.

    A lock timeout or deadlock is retried once after the day row is restored.
    """
    ensure_schema()
    day = resolve_day(day)
    ensure_day_row(day)
    outcome, seat = claim_transaction(day, int(job_id), int(user["sub"]))
    if outcome == "retry":
        ensure_day_row(day)
        outcome, seat = claim_transaction(day, int(job_id), int(user["sub"]))
    return outcome, seat, day
