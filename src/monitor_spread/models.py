"""Modelos de dados compartilhados entre os módulos do monitor.

Todos os modelos são dataclasses imutáveis e independentes de bibliotecas
externas, para que o calculator possa ser testado sem nenhuma dependência
de rede instalada.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Nível de order book: (preço, quantidade na moeda base).
Level = tuple[float, float]

# Nomes canônicos das exchanges monitoradas.
MERCADO_BITCOIN = "mercado_bitcoin"
FOXBIT = "foxbit"
BRASIL_BITCOIN = "brasil_bitcoin"
BINANCE = "binance"
REFERENCIA_USD = "referencia_usd"

BR_EXCHANGES: tuple[str, ...] = (MERCADO_BITCOIN, FOXBIT, BRASIL_BITCOIN)
# Venues de negociação: exchanges BR + Binance (que negocia pares /BRL
# diretamente, com taker bem menor — é a ponta que costuma abrir rota).
VENUES: tuple[str, ...] = (*BR_EXCHANGES, BINANCE)
BR_SYMBOLS: tuple[str, ...] = ("BTC/BRL", "USDT/BRL")

# Modos de execução de uma rota de arbitragem.
MODE_TRANSFER = "transferencia"  # compra -> transfere o ativo -> vende
MODE_PREPOSITIONED = "pre_posicionado"  # capital nas duas pontas, ordens simultâneas


@dataclass(frozen=True)
class OrderBook:
    """Snapshot de order book de uma exchange.

    Bids ordenados do maior para o menor preço; asks do menor para o maior.
    """

    exchange: str
    symbol: str
    bids: list[Level]
    asks: list[Level]
    timestamp: float

    @property
    def best_bid(self) -> float | None:
        """Melhor preço de compra do book (topo dos bids)."""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        """Melhor preço de venda do book (topo dos asks)."""
        return self.asks[0][0] if self.asks else None


@dataclass(frozen=True)
class UsdBrlRate:
    """Cotação USD/BRL (dólar comercial) usada para converter a referência."""

    bid: float
    ask: float
    timestamp: float


@dataclass(frozen=True)
class ReferencePrice:
    """Preço de referência internacional implícito em BRL para um par.

    ``buy_brl``: custo médio (VWAP) para *comprar* 1 unidade do ativo lá fora.
    ``sell_brl``: receita média (VWAP) para *vender* 1 unidade lá fora.
    """

    symbol: str
    buy_brl: float
    sell_brl: float


@dataclass(frozen=True)
class ExchangePremium:
    """Cotação de uma exchange BR e seu ágio frente à referência internacional.

    Os VWAPs consideram a execução de um notional configurável (capital em
    BRL), não apenas o topo do book. Campos de ágio ficam ``None`` quando a
    referência internacional está indisponível ou o book é raso demais.
    """

    exchange: str
    symbol: str
    best_bid: float | None
    best_ask: float | None
    bid_vwap: float | None
    ask_vwap: float | None
    agio_bruto_pct: float | None
    agio_liquido_pct: float | None


@dataclass(frozen=True)
class Opportunity:
    """Oportunidade de arbitragem: comprar em uma ponta e vender na outra.

    ``mode`` define a execução:
    - ``MODE_TRANSFER``: capital inteiro, compra -> transferência -> venda ->
      saque PIX; ``net_pct`` desconta todos esses custos.
    - ``MODE_PREPOSITIONED``: metade do capital em cada ponta, ordens
      simultâneas; ``net_pct`` desconta só as corretagens (o rebalanceamento
      posterior fica fora — grátis se houver janela inversa).

    ``net_pct`` e ``est_profit_brl`` são sempre relativos ao capital TOTAL,
    então os dois modos são comparáveis entre si e com o threshold.
    """

    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    gross_pct: float
    net_pct: float
    est_profit_brl: float
    mode: str = MODE_TRANSFER


@dataclass(frozen=True)
class CycleSnapshot:
    """Resultado completo de um ciclo de coleta + cálculo."""

    timestamp: float
    premiums: list[ExchangePremium] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)
    references: dict[str, ReferencePrice] = field(default_factory=dict)
