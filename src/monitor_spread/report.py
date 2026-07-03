"""Relatório de estatísticas sobre o histórico de spreads gravado no SQLite.

Uso:
    python -m monitor_spread.report [--db data/spreads.db] [--threshold 0.8]

Agrupa registros consecutivos acima do threshold em "janelas" de
oportunidade (por rota) e imprime: janelas por dia, duração média, spread
máximo, melhor horário e lucro teórico acumulado caso todas as janelas
tivessem sido capturadas (uma execução por janela).
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Window:
    """Janela contínua de oportunidade acima do threshold para uma rota."""

    symbol: str
    route: str
    start: float
    end: float
    max_net_pct: float
    max_profit_brl: float

    @property
    def duration_seconds(self) -> float:
        return self.end - self.start

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.start).strftime("%Y-%m-%d")


def load_windows(conn: sqlite3.Connection, threshold: float, max_gap_seconds: float) -> list[Window]:
    """Agrupa oportunidades consecutivas acima do threshold em janelas.

    Registros da mesma rota separados por mais de ``max_gap_seconds`` iniciam
    uma nova janela (a anterior "fechou").
    """
    rows = conn.execute(
        """
        SELECT ts, symbol,
               buy_exchange || ' -> ' || sell_exchange || ' [' || mode || ']' AS route,
               net_pct, est_profit_brl
        FROM opportunities
        WHERE net_pct > ?
        ORDER BY symbol, route, ts
        """,
        (threshold,),
    ).fetchall()

    windows: list[Window] = []
    current: Window | None = None
    for ts, symbol, route, net_pct, profit in rows:
        starts_new = (
            current is None
            or current.symbol != symbol
            or current.route != route
            or ts - current.end > max_gap_seconds
        )
        if starts_new:
            if current is not None:
                windows.append(current)
            current = Window(symbol, route, ts, ts, net_pct, profit)
        else:
            assert current is not None
            current.end = ts
            current.max_net_pct = max(current.max_net_pct, net_pct)
            current.max_profit_brl = max(current.max_profit_brl, profit)
    if current is not None:
        windows.append(current)
    return windows


def best_hour(conn: sqlite3.Connection, threshold: float) -> tuple[int, int] | None:
    """Hora do dia com mais registros acima do threshold: (hora, contagem)."""
    counter: Counter[int] = Counter()
    for (ts,) in conn.execute("SELECT ts FROM opportunities WHERE net_pct > ?", (threshold,)):
        counter[datetime.fromtimestamp(ts).hour] += 1
    if not counter:
        return None
    hour, count = counter.most_common(1)[0]
    return hour, count


def print_report(db_path: Path, threshold: float, max_gap_seconds: float, capital_brl: float) -> None:
    """Imprime o relatório completo no stdout."""
    conn = sqlite3.connect(db_path)
    try:
        total_snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        max_spread = conn.execute("SELECT MAX(net_pct) FROM opportunities").fetchone()[0]
        windows = load_windows(conn, threshold, max_gap_seconds)
        hour_stats = best_hour(conn, threshold)
    finally:
        conn.close()

    print(f"Relatório de spreads — {db_path}")
    print(f"Threshold: {threshold:.2f}% | snapshots gravados: {total_snapshots}")
    print("-" * 60)

    if not windows:
        print("Nenhuma janela acima do threshold registrada.")
        return

    per_day: dict[str, list[Window]] = defaultdict(list)
    for window in windows:
        per_day[window.day].append(window)

    print("Janelas acima do threshold por dia:")
    for day in sorted(per_day):
        day_windows = per_day[day]
        profit = sum(w.max_profit_brl for w in day_windows)
        print(f"  {day}: {len(day_windows)} janela(s), lucro teórico R$ {profit:,.2f}")

    avg_duration = sum(w.duration_seconds for w in windows) / len(windows)
    total_profit = sum(w.max_profit_brl for w in windows)
    best_window = max(windows, key=lambda w: w.max_net_pct)

    print("-" * 60)
    print(f"Total de janelas: {len(windows)}")
    print(f"Duração média das janelas: {avg_duration:.0f}s")
    print(f"Spread líquido máximo: {max_spread:.2f}% "
          f"({best_window.symbol} {best_window.route})")
    if hour_stats is not None:
        print(f"Melhor horário do dia: {hour_stats[0]:02d}h ({hour_stats[1]} registros acima do threshold)")
    print(f"Lucro teórico acumulado (1 execução por janela): R$ {total_profit:,.2f}")

    _print_tax_warning(windows, capital_brl)


def _print_tax_warning(windows: list[Window], capital_brl: float) -> None:
    """Alerta quando o volume teórico de vendas estoura a isenção mensal de IR.

    Cada janela executada implica uma venda de ~1 capital. Vendas acima de
    R$ 35.000/mês em exchanges nacionais perdem a isenção e o ganho paga 15%
    de IR (regra vigente em 2026; a MP 1.303 caducou).
    """
    per_month: dict[str, int] = defaultdict(int)
    for window in windows:
        per_month[window.day[:7]] += 1
    over = {month: n * capital_brl for month, n in per_month.items() if n * capital_brl > 35_000.0}
    if not over:
        return
    print("-" * 60)
    print("⚠️  IR: meses com vendas teóricas acima da isenção de R$ 35.000:")
    for month in sorted(over):
        print(f"  {month}: ~R$ {over[month]:,.2f} em vendas "
              f"({per_month[month]} execuções × R$ {capital_brl:,.2f})")
    print("  Acima da isenção, o ganho do mês paga 15% de IR — o lucro")
    print("  teórico acima NÃO desconta esse imposto.")


def main() -> None:
    """Ponto de entrada do console script ``monitor-spread-report``."""
    parser = argparse.ArgumentParser(description="Estatísticas do histórico de spreads")
    parser.add_argument("--db", type=Path, default=Path("data/spreads.db"), help="caminho do SQLite")
    parser.add_argument("--threshold", type=float, default=0.8, help="threshold de spread líquido (%%)")
    parser.add_argument(
        "--max-gap",
        type=float,
        default=30.0,
        help="intervalo máximo (s) entre registros para considerar a mesma janela",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=11_000.0,
        help="capital por execução (BRL), usado no aviso de IR",
    )
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"banco não encontrado: {args.db}")
    print_report(args.db, args.threshold, args.max_gap, args.capital)


if __name__ == "__main__":
    main()
