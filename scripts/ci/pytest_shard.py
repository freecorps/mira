"""Run one slice of the test suite: `MIRA_TEST_SHARD=2/3 pytest -p pytest_shard`.

CI loads this with `PYTHONPATH=scripts/ci` to spread one Python version's
suite over several runners. Each test belongs to exactly one shard, picked by
a stable hash of its node id, so the shards of a version together run every
test once, and every xdist worker of a shard agrees on the same slice. A
hash rather than contiguous ranges spreads the slow retry/backoff tests
evenly without a timing file to keep current.

Without MIRA_TEST_SHARD the plugin does nothing.
"""

from __future__ import annotations

import os
import zlib

import pytest


def _shard() -> tuple[int, int] | None:
    value = os.environ.get("MIRA_TEST_SHARD", "").strip()
    if not value:
        return None
    index, _, total = value.partition("/")
    try:
        shard, count = int(index), int(total)
    except ValueError:
        shard = count = 0
    if not 1 <= shard <= count:
        raise pytest.UsageError(f"MIRA_TEST_SHARD={value!r}: expected k/n with 1 <= k <= n")
    return shard, count


def shard_of(nodeid: str, count: int) -> int:
    """The 1-based shard a test belongs to when the suite is cut in `count`."""
    return zlib.crc32(nodeid.encode()) % count + 1


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = _shard()
    if shard is None:
        return
    index, count = shard
    keep, drop = [], []
    for item in items:
        (keep if shard_of(item.nodeid, count) == index else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep


def pytest_report_header(config: pytest.Config) -> str | None:
    shard = _shard()
    return None if shard is None else f"test shard: {shard[0]}/{shard[1]}"
