"""Loop principal do monitor: coleta -> cálculo -> persistência -> alerta.

Encerramento gracioso via SIGTERM/SIGINT: o ciclo em andamento termina,
recursos de rede e o banco são fechados antes de sair.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from .alerter import ConsoleAlerter
from .calculator import compute_cycle
from .collector import MarketDataCollector
from .config import Settings
from .storage import SpreadStorage

logger = logging.getLogger("monitor_spread")


def configure_logging(level: str) -> None:
    """Configura logging estruturado (chave=valor) no stdout."""
    logging.basicConfig(
        level=level.upper(),
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # httpx loga cada request em INFO; só interessa em depuração
    if level.upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_cycle(
    collector: MarketDataCollector,
    storage: SpreadStorage,
    alerter: ConsoleAlerter,
    settings: Settings,
) -> None:
    """Executa um ciclo completo de coleta, cálculo, gravação e alertas."""
    books, usd_brl = await collector.collect()
    snapshot = compute_cycle(books, usd_brl, settings.taker_fees(), settings.capital_brl)
    await storage.save(snapshot)

    best = snapshot.opportunities[0] if snapshot.opportunities else None
    logger.info(
        "ciclo exchanges=%d usd_brl=%s melhor_rota=%s liquido=%s",
        len(books),
        f"{usd_brl.ask:.4f}" if usd_brl else "indisponível",
        f"{best.symbol} {best.buy_exchange}->{best.sell_exchange}" if best else "-",
        f"{best.net_pct:.2f}%" if best else "-",
    )
    for opportunity in snapshot.opportunities:
        alerter.maybe_alert(opportunity)


async def run() -> None:
    """Inicializa os componentes e roda o loop até receber SIGTERM/SIGINT."""
    settings = Settings()
    configure_logging(settings.log_level)
    logger.info(
        "iniciando monitor pares=%s intervalo=%.0fs threshold=%.2f%% capital=%.2f transfer_fee_usdt=%.2f",
        ",".join(settings.symbol_list()),
        settings.poll_interval_seconds,
        settings.spread_threshold_pct,
        settings.capital_brl,
        settings.transfer_fee_usdt,
    )

    collector = MarketDataCollector(settings)
    storage = SpreadStorage(settings.db_path)
    alerter = ConsoleAlerter(settings)
    await storage.init()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                await run_cycle(collector, storage, alerter, settings)
            except Exception:
                logger.exception("erro inesperado no ciclo; seguindo para o próximo")
            elapsed = time.monotonic() - started
            wait = max(0.0, settings.poll_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except TimeoutError:
                pass
    finally:
        logger.info("encerrando: fechando conexões")
        await collector.close()
        await storage.close()


def main() -> None:
    """Ponto de entrada do console script ``monitor-spread``."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
