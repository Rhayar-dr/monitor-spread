"""Persistência do histórico de spreads em SQLite (via aiosqlite).

Cada ciclo grava um snapshot por exchange/par e as oportunidades calculadas.
Esse histórico é o dataset usado pelo ``report.py`` e por backtests futuros.
O banco também guarda o estado do capital do usuário (``balances``) e as
execuções marcadas no dashboard (``executions``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite

from .models import CycleSnapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    bid_vwap REAL,
    ask_vwap REAL,
    agio_bruto_pct REAL,
    agio_liquido_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (ts);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    sell_exchange TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'transferencia',
    buy_price REAL NOT NULL,
    sell_price REAL NOT NULL,
    gross_pct REAL NOT NULL,
    net_pct REAL NOT NULL,
    est_profit_brl REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opportunities_ts ON opportunities (ts);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    sell_exchange TEXT NOT NULL,
    mode TEXT NOT NULL,
    capital_brl REAL,
    net_pct REAL,
    est_profit_brl REAL,
    notional_brl REAL,
    qty REAL,
    proceeds_brl REAL
);

CREATE TABLE IF NOT EXISTS balances (
    exchange TEXT NOT NULL,
    asset TEXT NOT NULL,
    amount REAL NOT NULL,
    updated_ts REAL NOT NULL,
    PRIMARY KEY (exchange, asset)
);
"""

# Colunas adicionadas depois do schema original: (tabela, coluna, DDL).
# ``CREATE TABLE IF NOT EXISTS`` não altera tabelas existentes, então bancos
# antigos precisam do ALTER correspondente.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("opportunities", "mode",
     "ALTER TABLE opportunities ADD COLUMN mode TEXT NOT NULL DEFAULT 'transferencia'"),
    ("executions", "notional_brl", "ALTER TABLE executions ADD COLUMN notional_brl REAL"),
    ("executions", "qty", "ALTER TABLE executions ADD COLUMN qty REAL"),
    ("executions", "proceeds_brl", "ALTER TABLE executions ADD COLUMN proceeds_brl REAL"),
]


def apply_schema_sync(conn: sqlite3.Connection) -> None:
    """Cria o schema e aplica migrações numa conexão sqlite3 síncrona.

    Usado pelo dashboard, que acessa o banco fora do event loop do monitor.
    """
    conn.executescript(_SCHEMA)
    for table, column, ddl in _COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(ddl)
    conn.commit()


class SpreadStorage:
    """Gravação assíncrona dos snapshots de cada ciclo no SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Abre a conexão, cria o schema e aplica migrações leves."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Adiciona colunas novas a bancos criados por versões anteriores."""
        assert self._conn is not None
        for table, column, ddl in _COLUMN_MIGRATIONS:
            cursor = await self._conn.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in await cursor.fetchall()}
            if column not in columns:
                await self._conn.execute(ddl)

    async def save(self, snapshot: CycleSnapshot) -> None:
        """Grava os spreads e oportunidades de um ciclo."""
        assert self._conn is not None, "chame init() antes de save()"
        await self._conn.executemany(
            """
            INSERT INTO snapshots
                (ts, exchange, symbol, best_bid, best_ask, bid_vwap, ask_vwap,
                 agio_bruto_pct, agio_liquido_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot.timestamp,
                    p.exchange,
                    p.symbol,
                    p.best_bid,
                    p.best_ask,
                    p.bid_vwap,
                    p.ask_vwap,
                    p.agio_bruto_pct,
                    p.agio_liquido_pct,
                )
                for p in snapshot.premiums
            ],
        )
        await self._conn.executemany(
            """
            INSERT INTO opportunities
                (ts, symbol, buy_exchange, sell_exchange, mode, buy_price,
                 sell_price, gross_pct, net_pct, est_profit_brl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot.timestamp,
                    o.symbol,
                    o.buy_exchange,
                    o.sell_exchange,
                    o.mode,
                    o.buy_price,
                    o.sell_price,
                    o.gross_pct,
                    o.net_pct,
                    o.est_profit_brl,
                )
                for o in snapshot.opportunities
            ],
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Fecha a conexão com o banco."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
