"""Testes do schema e das migrações do storage."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from monitor_spread.storage import SpreadStorage, apply_schema_sync


def test_schema_sincrono_e_assincrono_convergem(tmp_path: Path) -> None:
    """O dashboard (sync) e o monitor (async) devem produzir o mesmo schema."""
    sync_db = tmp_path / "sync.db"
    conn = sqlite3.connect(sync_db)
    apply_schema_sync(conn)
    sync_tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()

    async def run() -> set[str]:
        storage = SpreadStorage(tmp_path / "async.db")
        await storage.init()
        try:
            assert storage._conn is not None
            cursor = await storage._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            return {row[0] for row in await cursor.fetchall()}
        finally:
            await storage.close()

    async_tables = asyncio.run(run())
    assert {"snapshots", "opportunities", "executions", "balances"} <= sync_tables
    assert sync_tables == async_tables


def test_migracao_adiciona_colunas_em_banco_antigo(tmp_path: Path) -> None:
    """Banco criado por versão antiga (sem colunas novas) deve ser migrado."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            symbol TEXT NOT NULL, buy_exchange TEXT NOT NULL,
            sell_exchange TEXT NOT NULL, mode TEXT NOT NULL,
            capital_brl REAL, net_pct REAL, est_profit_brl REAL
        )
        """
    )
    conn.commit()
    apply_schema_sync(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
    conn.close()
    assert {"notional_brl", "qty", "proceeds_brl"} <= columns
