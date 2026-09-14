
from types import SimpleNamespace

import pytest


from litellm.proxy.management_helpers.access_group_team_sync import (
    invalidate_access_group_caches,
    reconcile_team_access_group_membership,
)


def _recording_tx(team_row_result, calls):
    async def query_raw(sql, *args):
        calls.append((sql, args))
        if "LiteLLM_TeamTable" in sql:
            return team_row_result
        if sql.lstrip().startswith("SELECT"):
            return [{"access_group_id": "ag-1"}]
        return []

    return SimpleNamespace(query_raw=query_raw)


@pytest.mark.asyncio
async def test_reconcile_locks_the_team_row_as_its_first_statement():
    """
    The mirror's read must carry FOR UPDATE so the team row lock is held for the rest of
    the transaction: that row lock, not a Postgres advisory lock (which CockroachDB does
    not have), is what serializes concurrent reconciles for one team. It also has to be
    the transaction's first statement so nothing is computed off an unlocked snapshot.
    """
    calls = []
    tx = _recording_tx([{"access_group_ids": ["ag-1"]}], calls)

    affected = await reconcile_team_access_group_membership(tx, "team-1")

    first_sql, first_args = calls[0]
    assert "LiteLLM_TeamTable" in first_sql and "FOR UPDATE" in first_sql
    assert first_args == ("team-1",)
    assert not any("pg_advisory" in sql or "hashtext" in sql for sql, _ in calls)
    assert affected == ("ag-1",)


@pytest.mark.asyncio
async def test_reconcile_of_a_missing_team_still_runs_the_detach_pass():
    """
    A deleted team has no row, so FOR UPDATE locks nothing. The reconcile must not treat
    that as an error: it has to fall through to an empty desired set so the detach
    statement strips the team from every group, which is the /team/delete follow-up sync.
    """
    calls = []
    tx = _recording_tx([], calls)

    affected = await reconcile_team_access_group_membership(tx, "team-gone")

    attach_and_detach = [args for sql, args in calls if not sql.lstrip().startswith("SELECT")]
    assert attach_and_detach == [("team-gone", ()), ("team-gone", ())], (
        "attach and detach must both run against an empty desired set for a deleted team"
    )
    assert affected == ("ag-1",)


@pytest.mark.asyncio
async def test_one_unreachable_cache_does_not_skip_the_other_groups(monkeypatch):
    """
    `assigned_team_ids` is an authorization input, so a group whose cache still holds the
    revoked grant keeps serving it until the entry is dropped.

    A sequential loop would stop at the first failing group and leave the groups behind it
    serving stale grants, and swallowing the failure would report success to the admin for
    a revoke that never took effect. Every group has to be attempted, and the endpoint has
    to fail so the caller can retry.
    """
    attempted: list[str] = []

    async def _invalidate(access_group_id: str) -> None:
        attempted.append(access_group_id)
        if access_group_id == "ag-redis-down":
            raise ConnectionError("redis unreachable")

    monkeypatch.setattr(
        "litellm.proxy.management_helpers.access_group_team_sync.invalidate_access_group_cache",
        _invalidate,
    )

    with pytest.raises(ConnectionError):
        await invalidate_access_group_caches(("ag-redis-down", "ag-2", "ag-3"))

    assert attempted == ["ag-redis-down", "ag-2", "ag-3"]
