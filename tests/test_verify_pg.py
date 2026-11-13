"""Verification outcomes on the Postgres store, through the SQLite-backed stand-in."""

from __future__ import annotations

import dataclasses

from tests.test_pg_store import _pg_finding, fake_conn, store  # noqa: F401


def test_postgres_verification_round_trips_and_is_counted(store):  # noqa: F811
    _pg_finding(store, "plain")
    from mira.feedback.models import ReviewFinding

    base = store.get_review_finding("plain")
    assert base is not None and base.verification == ""
    verified: ReviewFinding = dataclasses.replace(
        base, id="verified", verification="reproduced", verification_detail="failed as described"
    )
    store.save_review_finding(verified)

    got = store.get_review_finding("verified")
    assert got is not None
    assert (got.verification, got.verification_detail) == ("reproduced", "failed as described")
    stats = {b["verification"]: b["findings"] for b in store.finding_verification_stats({})}
    assert stats == {"reproduced": 1, "unverified": 1}
