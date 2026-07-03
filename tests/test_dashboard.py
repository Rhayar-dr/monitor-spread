"""Testes do motor de capital e recomendação do dashboard."""

from __future__ import annotations

import math
import time
from pathlib import Path

from monitor_spread.config import Settings
from monitor_spread.dashboard import (
    _connect_rw,
    advice_payload,
    get_balances,
    register_execution,
    set_balance,
    undo_last_execution,
)
from monitor_spread.models import MODE_PREPOSITIONED

SETTINGS = Settings(_env_file=None)
FEE_FOXBIT = SETTINGS.fee_taker_foxbit
FEE_MB = SETTINGS.fee_taker_mercado_bitcoin


def seed_cycle(db: Path, net_pct: float = 1.0) -> None:
    """Grava um ciclo com uma rota foxbit -> mercado_bitcoin no banco."""
    conn = _connect_rw(db)
    ts = time.time()
    conn.execute(
        "INSERT INTO snapshots (ts, exchange, symbol) VALUES (?, 'foxbit', 'USDT/BRL')", (ts,)
    )
    conn.execute(
        """
        INSERT INTO opportunities
            (ts, symbol, buy_exchange, sell_exchange, mode, buy_price, sell_price,
             gross_pct, net_pct, est_profit_brl)
        VALUES (?, 'USDT/BRL', 'foxbit', 'mercado_bitcoin', ?, 5.20, 5.30, 1.9, ?, 55.0)
        """,
        (ts, MODE_PREPOSITIONED, net_pct),
    )
    conn.commit()
    conn.close()


def test_set_e_get_balances(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    assert set_balance(db, {"exchange": "foxbit", "asset": "BRL", "amount": 5500})["ok"]
    assert set_balance(db, {"exchange": "foxbit", "asset": "USDT", "amount": 100})["ok"]
    assert not set_balance(db, {"exchange": "foxbit", "asset": "DOGE", "amount": 1})["ok"]
    assert get_balances(db) == {"foxbit": {"BRL": 5500.0, "USDT": 100.0}}


def test_execucao_movimenta_os_quatro_bolsos(tmp_path: Path) -> None:
    """Comprar em A e vender em B move BRL e USDT nas duas pontas."""
    db = tmp_path / "t.db"
    set_balance(db, {"exchange": "foxbit", "asset": "BRL", "amount": 5500})
    set_balance(db, {"exchange": "mercado_bitcoin", "asset": "USDT", "amount": 1100})

    notional = 5000.0
    result = register_execution(
        db,
        {"symbol": "USDT/BRL", "buy_exchange": "foxbit", "sell_exchange": "mercado_bitcoin",
         "buy_price": 5.20, "sell_price": 5.30, "notional_brl": notional, "net_pct": 1.0},
        SETTINGS,
    )
    assert result["ok"]
    qty = notional / (5.20 * (1 + FEE_FOXBIT))
    proceeds = qty * 5.30 * (1 - FEE_MB)
    balances = get_balances(db)
    assert math.isclose(balances["foxbit"]["BRL"], 5500 - notional, rel_tol=1e-9)
    assert math.isclose(balances["foxbit"]["USDT"], qty, rel_tol=1e-9)
    assert math.isclose(balances["mercado_bitcoin"]["USDT"], 1100 - qty, rel_tol=1e-9)
    assert math.isclose(balances["mercado_bitcoin"]["BRL"], proceeds, rel_tol=1e-9)

    # Desfazer estorna tudo
    assert undo_last_execution(db)["removed"] == 1
    balances = get_balances(db)
    assert math.isclose(balances["foxbit"]["BRL"], 5500, rel_tol=1e-9)
    assert math.isclose(balances["foxbit"]["USDT"], 0, abs_tol=1e-9)
    assert math.isclose(balances["mercado_bitcoin"]["USDT"], 1100, rel_tol=1e-9)
    assert math.isclose(balances["mercado_bitcoin"]["BRL"], 0, abs_tol=1e-9)


def test_advice_dimensiona_pelo_saldo(tmp_path: Path) -> None:
    """O tamanho executável é min(BRL na compra, USDT na venda em BRL)."""
    db = tmp_path / "t.db"
    seed_cycle(db, net_pct=1.0)  # acima do threshold de 0.8
    set_balance(db, {"exchange": "foxbit", "asset": "BRL", "amount": 4000})
    set_balance(db, {"exchange": "mercado_bitcoin", "asset": "USDT", "amount": 500})

    advice = advice_payload(db, SETTINGS)
    route = advice["routes"][0]
    usdt_em_brl = 500 * 5.30 * (1 - FEE_MB)  # ~R$ 2.631: é o gargalo
    assert math.isclose(route["max_notional_brl"], usdt_em_brl, rel_tol=1e-9)
    assert route["executable"]
    assert advice["action"]["type"] == "execute"
    assert advice["action"]["route"]["buy_exchange"] == "foxbit"


def test_advice_sem_saldo_e_capital_no_lugar_errado(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    seed_cycle(db, net_pct=1.0)
    # Sem saldo nenhum: pede setup
    assert advice_payload(db, SETTINGS)["action"]["type"] == "setup"
    # Capital existe, mas do lado errado (BRL na ponta de venda)
    set_balance(db, {"exchange": "mercado_bitcoin", "asset": "BRL", "amount": 5500})
    advice = advice_payload(db, SETTINGS)
    assert advice["action"]["type"] == "capital_misplaced"
    assert "USDT na mercado_bitcoin" in advice["action"]["route"]["missing"]
    assert "BRL na foxbit" in advice["action"]["route"]["missing"]


def test_advice_spread_baixo_sem_pendencia(tmp_path: Path) -> None:
    """Spread abaixo do threshold e capital ok: ação é aguardar."""
    db = tmp_path / "t.db"
    seed_cycle(db, net_pct=0.1)
    set_balance(db, {"exchange": "foxbit", "asset": "BRL", "amount": 5500})
    set_balance(db, {"exchange": "mercado_bitcoin", "asset": "USDT", "amount": 1100})
    advice = advice_payload(db, SETTINGS)
    # 0.1% > threshold reduzido (0%) e a rota reequilibra? foxbit tem mais BRL
    # que mercado_bitcoin, então executar reequilibra -> ação execute.
    assert advice["action"]["type"] == "execute"

    # Se o BRL já está concentrado na ponta de venda, 0.1% não basta
    set_balance(db, {"exchange": "mercado_bitcoin", "asset": "BRL", "amount": 9000})
    advice = advice_payload(db, SETTINGS)
    assert advice["action"]["type"] == "none"
