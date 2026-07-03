"""Alertas de oportunidade impressos no console, com cooldown por rota."""

from __future__ import annotations

import logging
import time

from .config import Settings
from .models import Opportunity

logger = logging.getLogger(__name__)


def format_brl(value: float) -> str:
    """Formata um valor em reais no padrão brasileiro (R$ 1.234,56)."""
    text = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {text}"


def format_message(opportunity: Opportunity, capital_brl: float) -> str:
    """Monta o texto do alerta como uma instrução de ação, não um relatório."""
    leg = capital_brl / 2.0
    return (
        f"🚨 JANELA ABERTA — {opportunity.symbol} · capital pré-posicionado\n"
        f"   COMPRE na {opportunity.buy_exchange} @ {format_brl(opportunity.buy_price)}\n"
        f"   VENDA na  {opportunity.sell_exchange} @ {format_brl(opportunity.sell_price)}\n"
        f"   Spread líquido: {opportunity.net_pct:+.2f}% "
        f"(lucro {format_brl(opportunity.est_profit_brl)} p/ perna de {format_brl(leg)})\n"
        f"   Dimensione pelo teu saldo real no dashboard — ele diz quanto dá\n"
        f"   para executar e atualiza teu capital quando você confirmar.\n"
        f"   (desconta corretagens; rebalanceamento e IR fora — ver README)"
    )


class ConsoleAlerter:
    """Imprime alertas no console quando o spread líquido supera o threshold.

    Mantém um cooldown por rota (par + exchanges) para não repetir o mesmo
    alerta a cada ciclo enquanto a janela continua aberta.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last_sent: dict[str, float] = {}

    @staticmethod
    def _route_key(opportunity: Opportunity) -> str:
        return (
            f"{opportunity.symbol}:{opportunity.mode}:"
            f"{opportunity.buy_exchange}->{opportunity.sell_exchange}"
        )

    def _in_cooldown(self, key: str, now: float) -> bool:
        last = self._last_sent.get(key)
        return last is not None and (now - last) < self._settings.alert_cooldown_minutes * 60.0

    def maybe_alert(self, opportunity: Opportunity, now: float | None = None) -> bool:
        """Imprime o alerta se o spread supera o threshold e não há cooldown.

        Returns:
            ``True`` se um alerta foi impresso neste ciclo.
        """
        if opportunity.net_pct <= self._settings.spread_threshold_pct:
            return False
        current = time.time() if now is None else now
        key = self._route_key(opportunity)
        if self._in_cooldown(key, current):
            logger.debug("alerta suprimido por cooldown: %s", key)
            return False

        banner = "=" * 62
        message = format_message(opportunity, self._settings.capital_brl)
        print(f"\n{banner}\n{message}\n{banner}\n", flush=True)
        self._last_sent[key] = current
        logger.info("alerta emitido: %s (líquido %.2f%%)", key, opportunity.net_pct)
        return True
