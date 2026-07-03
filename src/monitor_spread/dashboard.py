"""Dashboard web do monitor: spreads ao vivo + gestão do capital do usuário.

Servidor HTTP simples (stdlib), separado do processo do monitor — lê o banco
que o monitor escreve e mantém o estado do capital (saldos de BRL e USDT por
exchange). A recomendação de ação ("compre em X, venda em Y, tamanho N") é
dimensionada pelo saldo real; marcar uma execução atualiza os saldos — o
mesmo modelo que a v2 usará para gerenciar automaticamente.

Uso:
    monitor-spread-dashboard [--port 8000] [--db data/spreads.db]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .models import MODE_PREPOSITIONED
from .storage import apply_schema_sync

logger = logging.getLogger(__name__)

HTML_PATH = Path(__file__).parent / "dashboard.html"

# Notional mínimo (BRL) para uma rota contar como executável.
MIN_TRADE_BRL = 50.0

BRL = "BRL"
USDT = "USDT"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Abre o banco em modo somente-leitura, tolerante a escrita concorrente."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    """Conexão de escrita (saldos e execuções), com schema garantido."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    apply_schema_sync(conn)
    return conn


# ---------------------------------------------------------------------------
# Leitura do histórico do monitor


def latest_payload(db_path: Path) -> dict[str, Any]:
    """Último ciclo gravado: cotações/ágios por exchange e todas as rotas."""
    conn = _connect(db_path)
    try:
        ts = conn.execute("SELECT MAX(ts) AS ts FROM snapshots").fetchone()["ts"]
        if ts is None:
            return {"ts": None, "premiums": [], "opportunities": []}
        premiums = [
            dict(row)
            for row in conn.execute(
                """
                SELECT exchange, symbol, best_bid, best_ask, bid_vwap, ask_vwap,
                       agio_bruto_pct, agio_liquido_pct
                FROM snapshots WHERE ts = ? ORDER BY symbol, exchange
                """,
                (ts,),
            )
        ]
        opportunities = [
            dict(row)
            for row in conn.execute(
                """
                SELECT symbol, buy_exchange, sell_exchange, mode, buy_price,
                       sell_price, gross_pct, net_pct, est_profit_brl
                FROM opportunities WHERE ts = ? AND mode = ?
                ORDER BY net_pct DESC
                """,
                (ts, MODE_PREPOSITIONED),
            )
        ]
        return {"ts": ts, "premiums": premiums, "opportunities": opportunities}
    finally:
        conn.close()


def history_payload(db_path: Path, hours: float) -> dict[str, Any]:
    """Série temporal do melhor spread líquido nas últimas N horas."""
    since = time.time() - hours * 3600.0
    conn = _connect(db_path)
    try:
        points = [
            [row["ts"], row["best_net"]]
            for row in conn.execute(
                """
                SELECT ts, MAX(net_pct) AS best_net
                FROM opportunities WHERE ts > ? AND mode = ?
                GROUP BY ts ORDER BY ts
                """,
                (since, MODE_PREPOSITIONED),
            )
        ]
        return {"series": points}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Capital do usuário


def get_balances(db_path: Path) -> dict[str, dict[str, float]]:
    """Saldos por exchange: ``{exchange: {"BRL": x, "USDT": y}}``."""
    conn = _connect_rw(db_path)
    try:
        balances: dict[str, dict[str, float]] = {}
        for row in conn.execute("SELECT exchange, asset, amount FROM balances"):
            balances.setdefault(row["exchange"], {})[row["asset"]] = row["amount"]
        return balances
    finally:
        conn.close()


def set_balance(db_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Define o saldo de um ativo em uma exchange (edição manual do usuário)."""
    exchange = payload.get("exchange")
    asset = payload.get("asset")
    try:
        amount = float(payload.get("amount", ""))
    except (TypeError, ValueError):
        return {"ok": False, "error": "amount inválido"}
    if not exchange or asset not in (BRL, USDT) or amount < 0:
        return {"ok": False, "error": "esperado: exchange, asset (BRL|USDT), amount >= 0"}
    conn = _connect_rw(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO balances (exchange, asset, amount, updated_ts) VALUES (?, ?, ?, ?)",
            (exchange, asset, amount, time.time()),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _adjust_balance(conn: sqlite3.Connection, exchange: str, asset: str, delta: float) -> None:
    conn.execute(
        """
        INSERT INTO balances (exchange, asset, amount, updated_ts)
        VALUES (?, ?, MAX(0, ?), ?)
        ON CONFLICT (exchange, asset)
        DO UPDATE SET amount = MAX(0, amount + ?), updated_ts = ?
        """,
        (exchange, asset, delta, time.time(), delta, time.time()),
    )


def register_execution(db_path: Path, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Registra uma execução e movimenta os saldos das duas pontas.

    Compra na ponta A: sai BRL, entra USDT (fica lá). Venda na ponta B: sai
    USDT, entra BRL. É o mesmo lançamento que a v2 automatizada fará.
    """
    required = ("symbol", "buy_exchange", "sell_exchange", "buy_price", "sell_price")
    if any(payload.get(k) in (None, "") for k in required):
        return {"ok": False, "error": f"campos obrigatórios: {', '.join(required)}"}
    try:
        notional = float(payload["notional_brl"])
        buy_price = float(payload["buy_price"])
        sell_price = float(payload["sell_price"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "notional_brl/buy_price/sell_price numéricos são obrigatórios"}
    if notional <= 0:
        return {"ok": False, "error": "notional_brl deve ser positivo"}

    fees = settings.taker_fees()
    buy_ex, sell_ex = payload["buy_exchange"], payload["sell_exchange"]
    qty = notional / (buy_price * (1.0 + fees.get(buy_ex, 0.0)))
    proceeds = qty * sell_price * (1.0 - fees.get(sell_ex, 0.0))

    conn = _connect_rw(db_path)
    try:
        with conn:  # transação: saldos + registro andam juntos
            _adjust_balance(conn, buy_ex, BRL, -notional)
            _adjust_balance(conn, buy_ex, USDT, qty)
            _adjust_balance(conn, sell_ex, USDT, -qty)
            _adjust_balance(conn, sell_ex, BRL, proceeds)
            conn.execute(
                """
                INSERT INTO executions
                    (ts, symbol, buy_exchange, sell_exchange, mode, capital_brl,
                     net_pct, est_profit_brl, notional_brl, qty, proceeds_brl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    payload["symbol"],
                    buy_ex,
                    sell_ex,
                    MODE_PREPOSITIONED,
                    settings.capital_brl,
                    payload.get("net_pct"),
                    proceeds - notional,
                    notional,
                    qty,
                    proceeds,
                ),
            )
        return {"ok": True, "qty": qty, "proceeds_brl": proceeds, "profit_brl": proceeds - notional}
    finally:
        conn.close()


def undo_last_execution(db_path: Path) -> dict[str, Any]:
    """Desfaz a última execução: remove o registro e estorna os saldos."""
    conn = _connect_rw(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM executions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"ok": True, "removed": 0}
        with conn:
            if row["notional_brl"] is not None:
                _adjust_balance(conn, row["buy_exchange"], BRL, row["notional_brl"])
                _adjust_balance(conn, row["buy_exchange"], USDT, -(row["qty"] or 0.0))
                _adjust_balance(conn, row["sell_exchange"], USDT, row["qty"] or 0.0)
                _adjust_balance(conn, row["sell_exchange"], BRL, -(row["proceeds_brl"] or 0.0))
            conn.execute("DELETE FROM executions WHERE id = ?", (row["id"],))
        return {"ok": True, "removed": 1}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Motor de recomendação


def advice_payload(db_path: Path, settings: Settings) -> dict[str, Any]:
    """Cruza os spreads do último ciclo com o saldo real e diz o que fazer.

    Para cada rota A->B: o tamanho executável é ``min(BRL em A, USDT em B
    valorado no preço de venda)``. A melhor ação é a rota executável com
    maior lucro cujo spread supera o threshold — ou, se ela ainda
    reconcentra o capital (BRL indo para a ponta que tem menos BRL), o
    threshold reduzido de rebalanceamento.
    """
    latest = latest_payload(db_path)
    balances = get_balances(db_path)
    fees = settings.taker_fees()
    has_capital = any(v > 0 for by_asset in balances.values() for v in by_asset.values())

    routes: list[dict[str, Any]] = []
    for opp in latest["opportunities"]:
        buy_ex, sell_ex = opp["buy_exchange"], opp["sell_exchange"]
        brl_available = balances.get(buy_ex, {}).get(BRL, 0.0)
        usdt_available = balances.get(sell_ex, {}).get(USDT, 0.0)
        usdt_value_brl = usdt_available * opp["sell_price"] * (1.0 - fees.get(sell_ex, 0.0))
        max_notional = min(brl_available, usdt_value_brl)
        executable = max_notional >= MIN_TRADE_BRL
        # Executar move BRL de A para B: rebalanceia se A tem mais BRL que B
        rebalancing = brl_available > balances.get(sell_ex, {}).get(BRL, 0.0)
        missing = []
        if brl_available < MIN_TRADE_BRL:
            missing.append(f"BRL na {buy_ex}")
        if usdt_value_brl < MIN_TRADE_BRL:
            missing.append(f"USDT na {sell_ex}")
        routes.append(
            {
                **opp,
                "max_notional_brl": max_notional,
                "max_profit_brl": max_notional * opp["net_pct"] / 100.0,
                "executable": executable,
                "rebalancing": rebalancing,
                "missing": missing,
            }
        )

    best = None
    for route in routes:
        bar = (
            settings.reverse_alert_threshold_pct
            if route["rebalancing"]
            else settings.spread_threshold_pct
        )
        if route["executable"] and route["net_pct"] > bar:
            if best is None or route["max_profit_brl"] > best["max_profit_brl"]:
                best = route

    if not has_capital:
        action = {"type": "setup"}
    elif best is not None:
        action = {"type": "execute", "route": best}
    else:
        # Há spread bom mas sem saldo do lado certo?
        blocked = next(
            (r for r in routes if not r["executable"] and r["net_pct"] > settings.spread_threshold_pct),
            None,
        )
        action = (
            {"type": "capital_misplaced", "route": blocked}
            if blocked
            else {"type": "none"}
        )

    return {"ts": latest["ts"], "routes": routes, "action": action, "balances": balances}


def inventory_payload(db_path: Path) -> dict[str, Any]:
    """Saldos + execuções recentes, para o painel de capital."""
    conn = _connect_rw(db_path)
    try:
        executions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, ts, symbol, buy_exchange, sell_exchange,
                       notional_brl, qty, proceeds_brl
                FROM executions ORDER BY id DESC LIMIT 8
                """
            )
        ]
    finally:
        conn.close()
    return {"balances": get_balances(db_path), "executions": executions}


# ---------------------------------------------------------------------------
# HTTP


class DashboardHandler(BaseHTTPRequestHandler):
    """Roteia a página e os endpoints JSON do dashboard."""

    db_path: Path
    settings: Settings

    def do_GET(self) -> None:  # noqa: N802 (nome exigido pela stdlib)
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send(200, "text/html; charset=utf-8", HTML_PATH.read_bytes())
            elif parsed.path == "/api/latest":
                self._send_json(latest_payload(self.db_path))
            elif parsed.path == "/api/history":
                hours = float(parse_qs(parsed.query).get("hours", ["6"])[0])
                self._send_json(history_payload(self.db_path, min(hours, 168.0)))
            elif parsed.path == "/api/advice":
                self._send_json(advice_payload(self.db_path, self.settings))
            elif parsed.path == "/api/inventory":
                self._send_json(inventory_payload(self.db_path))
            elif parsed.path == "/api/config":
                self._send_json(
                    {
                        "threshold_pct": self.settings.spread_threshold_pct,
                        "reverse_threshold_pct": self.settings.reverse_alert_threshold_pct,
                        "capital_brl": self.settings.capital_brl,
                        "poll_interval_seconds": self.settings.poll_interval_seconds,
                        "symbols": list(self.settings.symbol_list()),
                        "exchanges": ["mercado_bitcoin", "foxbit", "brasil_bitcoin"],
                    }
                )
            else:
                self._send(404, "text/plain; charset=utf-8", b"nao encontrado")
        except sqlite3.OperationalError as exc:
            # Banco ainda não existe ou está momentaneamente travado
            self._send_json(
                {"error": str(exc), "ts": None, "premiums": [], "opportunities": [],
                 "series": [], "routes": [], "action": {"type": "none"}, "balances": {}}
            )

    def do_POST(self) -> None:  # noqa: N802 (nome exigido pela stdlib)
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "JSON inválido"})
            return
        try:
            if self.path == "/api/balances":
                self._send_json(set_balance(self.db_path, payload))
            elif self.path == "/api/executions":
                self._send_json(register_execution(self.db_path, payload, self.settings))
            elif self.path == "/api/executions/undo":
                self._send_json(undo_last_execution(self.db_path))
            else:
                self._send(404, "text/plain; charset=utf-8", b"nao encontrado")
        except sqlite3.OperationalError as exc:
            self._send_json({"ok": False, "error": str(exc)})

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send(200, "application/json; charset=utf-8", json.dumps(payload).encode())

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("http %s", fmt % args)


def serve(db_path: Path, port: int, settings: Settings) -> None:
    """Sobe o servidor HTTP do dashboard (bloqueante; Ctrl+C para parar)."""
    handler = type("Handler", (DashboardHandler,), {"db_path": db_path, "settings": settings})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    logger.info("dashboard em http://localhost:%d (banco: %s)", port, db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    """Ponto de entrada do console script ``monitor-spread-dashboard``."""
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Dashboard web do monitor-spread")
    parser.add_argument("--port", type=int, default=settings.dashboard_port, help="porta HTTP")
    parser.add_argument("--db", type=Path, default=settings.db_path, help="caminho do SQLite")
    args = parser.parse_args()
    serve(args.db, args.port, settings)


if __name__ == "__main__":
    main()
