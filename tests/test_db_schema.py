from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from chat_parser import db


def pool_for(execute):
    @asynccontextmanager
    async def acquire():
        yield SimpleNamespace(execute=execute)

    return SimpleNamespace(acquire=acquire)


@pytest.mark.parametrize("failure_at", ["schema", "table_check"])
async def test_schema_reconnects_after_connection_is_lost(monkeypatch, failure_at):
    lost = asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation")
    old_execute = AsyncMock(side_effect=lost if failure_at == "schema" else None)
    new_execute = AsyncMock()
    get_pool = AsyncMock(side_effect=[pool_for(old_execute), pool_for(new_execute)])
    check = AsyncMock(side_effect=[lost, []] if failure_at == "table_check" else [[]])
    close = AsyncMock()
    monkeypatch.setattr(db, "get_pool", get_pool)
    monkeypatch.setattr(db, "missing_tables", check)
    monkeypatch.setattr(db, "close_pool", close)
    monkeypatch.setattr(db, "SCHEMA_RETRY_DELAY", 0)

    assert await db.apply_schema() == []
    assert get_pool.await_count == 2
    close.assert_awaited_once()
    assert old_execute.call_args == new_execute.call_args


async def test_schema_reconnect_is_bounded(monkeypatch):
    lost = asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation")
    get_pool = AsyncMock(return_value=pool_for(AsyncMock(side_effect=lost)))
    monkeypatch.setattr(db, "get_pool", get_pool)
    monkeypatch.setattr(db, "close_pool", AsyncMock())
    monkeypatch.setattr(db, "SCHEMA_RETRY_DELAY", 0)

    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        await db.apply_schema()
    assert get_pool.await_count == 2
    assert "Session pooler" in db.explain_error(lost)
    assert "активного проекта" in db.explain_error(str(lost))


async def test_schema_permission_errors_are_reported_without_retry(monkeypatch):
    execute = AsyncMock(side_effect=asyncpg.InsufficientPrivilegeError("permission denied"))
    get_pool = AsyncMock(return_value=pool_for(execute))
    close = AsyncMock()
    monkeypatch.setattr(db, "get_pool", get_pool)
    monkeypatch.setattr(db, "close_pool", close)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await db.apply_schema()
    get_pool.assert_awaited_once()
    close.assert_not_awaited()
