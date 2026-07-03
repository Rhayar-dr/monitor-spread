"""Configuração do monitor via variáveis de ambiente / arquivo .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import BINANCE, BRASIL_BITCOIN, FOXBIT, MERCADO_BITCOIN, REFERENCIA_USD


class Settings(BaseSettings):
    """Parâmetros de operação do monitor.

    Todos os campos podem ser sobrescritos por variáveis de ambiente com o
    mesmo nome em maiúsculas (ex.: ``SPREAD_THRESHOLD_PCT=1.2``) ou por um
    arquivo ``.env`` na raiz do projeto.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Pares monitorados (separados por vírgula; ex.: "USDT/BRL,BTC/BRL")
    symbols: str = "USDT/BRL"

    # Taxa fixa de transferência do USDT entre exchanges, em unidades de USDT.
    # Default: saque TRC-20 na Binance (~1 USDT). Confira na tela de saque!
    transfer_fee_usdt: float = 1.0

    # Ciclo de coleta
    poll_interval_seconds: float = 10.0
    request_timeout_seconds: float = 8.0
    retry_attempts: int = 3
    retry_base_delay_seconds: float = 0.5

    # Regras de alerta
    spread_threshold_pct: float = 0.8
    alert_cooldown_minutes: float = 15.0
    capital_brl: float = 11_000.0

    # Threshold reduzido para rotas que rebalanceiam inventário pendente:
    # a rota inversa devolve o capital ao lugar de graça, então vale alertar
    # com qualquer lucro positivo (e ela ainda economiza o rebalanceamento
    # pago que seria necessário de outra forma).
    reverse_alert_threshold_pct: float = 0.0

    # Persistência e logs
    db_path: Path = Path("data/spreads.db")
    log_level: str = "INFO"

    # Porta HTTP do dashboard web (monitor-spread-dashboard)
    dashboard_port: int = 8000

    # Taxas taker por exchange, em fração (0.007 = 0,70%)
    fee_taker_mercado_bitcoin: float = 0.007
    fee_taker_foxbit: float = 0.005
    fee_taker_brasil_bitcoin: float = 0.005
    fee_taker_binance: float = 0.001

    # Taxa de saque BRL (PIX) por exchange: fração + parcela fixa em BRL.
    # Cobrada na ponta de venda para devolver o capital à conta bancária.
    fee_saque_brl_pct_mercado_bitcoin: float = 0.0
    fee_saque_brl_fixo_mercado_bitcoin: float = 0.0
    fee_saque_brl_pct_foxbit: float = 0.0
    fee_saque_brl_fixo_foxbit: float = 0.0
    fee_saque_brl_pct_brasil_bitcoin: float = 0.005  # 0,5% + R$ 1,99
    fee_saque_brl_fixo_brasil_bitcoin: float = 1.99

    def symbol_list(self) -> tuple[str, ...]:
        """Pares monitorados, normalizados a partir da string do .env."""
        return tuple(s.strip().upper() for s in self.symbols.split(",") if s.strip())

    def transfer_fees(self) -> dict[str, float]:
        """Taxa fixa de transferência por par, em unidades da moeda base."""
        return {"USDT/BRL": self.transfer_fee_usdt}

    def cashout_fees(self) -> dict[str, tuple[float, float]]:
        """Taxa de saque BRL por exchange, no formato ``(fração, fixo em BRL)``."""
        return {
            MERCADO_BITCOIN: (self.fee_saque_brl_pct_mercado_bitcoin, self.fee_saque_brl_fixo_mercado_bitcoin),
            FOXBIT: (self.fee_saque_brl_pct_foxbit, self.fee_saque_brl_fixo_foxbit),
            BRASIL_BITCOIN: (self.fee_saque_brl_pct_brasil_bitcoin, self.fee_saque_brl_fixo_brasil_bitcoin),
        }

    def taker_fees(self) -> dict[str, float]:
        """Mapa exchange -> taxa taker, no formato esperado pelo calculator."""
        return {
            MERCADO_BITCOIN: self.fee_taker_mercado_bitcoin,
            FOXBIT: self.fee_taker_foxbit,
            BRASIL_BITCOIN: self.fee_taker_brasil_bitcoin,
            BINANCE: self.fee_taker_binance,
            # A "compra" de USDT na referência é a própria cotação USD/BRL;
            # não há taxa de corretagem associada.
            REFERENCIA_USD: 0.0,
        }
