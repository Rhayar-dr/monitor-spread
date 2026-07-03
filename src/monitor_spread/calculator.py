"""Cálculo de ágio e spreads líquidos a partir dos order books coletados.

Este módulo é puro (sem I/O e sem dependências externas) de propósito:
toda a matemática do monitor vive aqui e é coberta pelos testes unitários.

Convenções:
- VWAP ("preço médio ponderado") é sempre calculado para executar um
  notional em BRL, caminhando pelos níveis do book em vez de usar só o
  topo. Book raso demais para o notional -> ``None``.
- A estratégia é SEMPRE capital pré-posicionado (modo 2): saldo nas duas
  pontas, ordens simultâneas, sem transferência no caminho crítico. As
  rotas gravadas usam o notional padrão (metade do capital) para o dataset
  ficar comparável ao longo do tempo; o dimensionamento pelo saldo real
  acontece na camada de recomendação (dashboard).
- ``route_economics`` continua disponível para precificar o rebalanceamento
  PAGO (transferência TRC-20 + saque PIX), usado como estimativa de custo
  quando o capital está no lugar errado.
- O ágio vs. referência internacional é termômetro e fica nos ``premiums``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from itertools import permutations

from .models import (
    BINANCE,
    MODE_PREPOSITIONED,
    REFERENCIA_USD,
    VENUES,
    CycleSnapshot,
    ExchangePremium,
    Level,
    Opportunity,
    OrderBook,
    ReferencePrice,
    UsdBrlRate,
)

_EPS = 1e-9


def vwap_for_notional(levels: Sequence[Level], notional: float) -> float | None:
    """Preço médio ponderado para executar ``notional`` (na moeda de cotação).

    Caminha pelos níveis do book acumulando valor até completar o notional.
    Retorna ``None`` se o book não tem liquidez suficiente (book raso) ou se
    o notional é inválido.

    Args:
        levels: níveis (preço, quantidade) já ordenados do melhor para o pior.
        notional: valor a executar, na moeda de cotação (ex.: BRL, USDT).
    """
    if notional <= 0 or not levels:
        return None
    remaining = notional
    quantity = 0.0
    for price, amount in levels:
        if price <= 0 or amount <= 0:
            continue
        level_value = price * amount
        taken = min(level_value, remaining)
        quantity += taken / price
        remaining -= taken
        if remaining <= _EPS:
            break
    if remaining > _EPS or quantity <= 0:
        return None
    return notional / quantity


def net_spread_pct(buy_price: float, sell_price: float, buy_fee: float, sell_fee: float) -> float:
    """Spread líquido (%) de comprar a ``buy_price`` e vender a ``sell_price``.

    Desconta as taxas taker das duas pontas na forma multiplicativa.
    """
    return ((sell_price * (1.0 - sell_fee)) / (buy_price * (1.0 + buy_fee)) - 1.0) * 100.0


def gross_spread_pct(buy_price: float, sell_price: float) -> float:
    """Spread bruto (%) entre preço de venda e de compra, sem taxas."""
    return (sell_price / buy_price - 1.0) * 100.0


def btc_reference_price(
    binance_book: OrderBook | None,
    usd_brl: UsdBrlRate | None,
    capital_brl: float,
) -> ReferencePrice | None:
    """Preço de referência internacional do BTC em BRL.

    Combina o VWAP do book BTC/USDT da Binance (para o notional equivalente
    ao capital) com a cotação USD/BRL. Retorna ``None`` se qualquer insumo
    estiver indisponível ou o book for raso.
    """
    if binance_book is None or usd_brl is None:
        return None
    notional_buy_usdt = capital_brl / usd_brl.ask
    notional_sell_usdt = capital_brl / usd_brl.bid
    ask_vwap = vwap_for_notional(binance_book.asks, notional_buy_usdt)
    bid_vwap = vwap_for_notional(binance_book.bids, notional_sell_usdt)
    if ask_vwap is None or bid_vwap is None:
        return None
    return ReferencePrice(
        symbol="BTC/BRL",
        buy_brl=ask_vwap * usd_brl.ask,
        sell_brl=bid_vwap * usd_brl.bid,
    )


def usdt_reference_price(usd_brl: UsdBrlRate | None) -> ReferencePrice | None:
    """Preço de referência do USDT em BRL (aproximação USDT ~= USD)."""
    if usd_brl is None:
        return None
    return ReferencePrice(symbol="USDT/BRL", buy_brl=usd_brl.ask, sell_brl=usd_brl.bid)


def exchange_premium(
    book: OrderBook,
    reference: ReferencePrice | None,
    fee: float,
    reference_fee: float,
    capital_brl: float,
) -> ExchangePremium:
    """Ágio de uma exchange BR frente à referência internacional.

    O ágio bruto compara o VWAP de *venda* local (bids) com o custo de
    *compra* internacional — é o que uma operação de arbitragem
    internacional capturaria. O líquido desconta a taxa taker local e a
    taxa da ponta internacional.
    """
    bid_vwap = vwap_for_notional(book.bids, capital_brl)
    ask_vwap = vwap_for_notional(book.asks, capital_brl)
    agio_bruto: float | None = None
    agio_liquido: float | None = None
    if reference is not None and bid_vwap is not None:
        agio_bruto = gross_spread_pct(reference.buy_brl, bid_vwap)
        agio_liquido = net_spread_pct(reference.buy_brl, bid_vwap, reference_fee, fee)
    return ExchangePremium(
        exchange=book.exchange,
        symbol=book.symbol,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        bid_vwap=bid_vwap,
        ask_vwap=ask_vwap,
        agio_bruto_pct=agio_bruto,
        agio_liquido_pct=agio_liquido,
    )


def route_economics(
    buy_price: float,
    sell_price: float,
    capital_brl: float,
    buy_fee: float,
    sell_fee: float,
    transfer_fee_base: float = 0.0,
    cashout_pct: float = 0.0,
    cashout_fixed_brl: float = 0.0,
) -> tuple[float, float] | None:
    """Lucro real do ciclo completo de uma rota: (% do capital, BRL).

    Modela o fluxo de caixa de ponta a ponta, na ordem em que o dinheiro
    anda (depósito PIX na compra é gratuito nas três exchanges):

    1. Compra na ponta A: ``qty = capital / (preço_compra × (1 + taxa_compra))``
    2. Transferência do ativo: ``qty -= taxa_transferência`` (em unidades)
    3. Venda na ponta B: ``receita = qty × preço_venda × (1 − taxa_venda)``
    4. Saque do BRL: ``receita × (1 − pct_saque) − fixo_saque``

    Retorna ``None`` se a transferência consome toda a quantidade comprada.
    """
    quantity = capital_brl / (buy_price * (1.0 + buy_fee))
    quantity -= transfer_fee_base
    if quantity <= 0:
        return None
    receipts = quantity * sell_price * (1.0 - sell_fee)
    receipts = receipts * (1.0 - cashout_pct) - cashout_fixed_brl
    profit = receipts - capital_brl
    return profit / capital_brl * 100.0, profit


def mode2_economics(
    buy_price: float,
    sell_price: float,
    notional_brl: float,
    buy_fee: float,
    sell_fee: float,
) -> tuple[float, float]:
    """Captura pré-posicionada: (spread líquido % do notional, lucro em BRL).

    BRL parado na ponta de compra, USDT parado na ponta de venda; quando a
    janela abre, as duas ordens disparam simultaneamente — sem transferência
    nem saque no caminho crítico, então a janela só precisa durar o tempo de
    duas chamadas de API.

    Só as corretagens taker entram aqui. O rebalanceamento posterior fica
    de fora de propósito: ele é grátis quando uma janela no sentido inverso
    aparece, e custa transferência + saque quando feito ativamente — é um
    custo de gestão de inventário, não da captura.
    """
    quantity = notional_brl / (buy_price * (1.0 + buy_fee))
    profit = quantity * sell_price * (1.0 - sell_fee) - notional_brl
    return profit / notional_brl * 100.0, profit


def cross_exchange_opportunities(
    books: Mapping[str, OrderBook],
    fees: Mapping[str, float],
    capital_brl: float,
) -> list[Opportunity]:
    """Oportunidades pré-posicionadas entre as exchanges BR para um par.

    Para cada par ordenado (comprar em A, vender em B) calcula a captura
    simultânea com o notional padrão de uma perna (metade do capital) — o
    tamanho de referência do dataset. O dimensionamento pelo saldo real de
    cada exchange acontece na camada de recomendação.

    Exchanges ausentes (fora do ar) ou com book raso para a perna são
    ignoradas; rotas são ordenadas pelo spread líquido, da melhor à pior.
    """
    opportunities: list[Opportunity] = []
    leg = capital_brl / 2.0
    for buy_name, sell_name in permutations(books, 2):
        buy_book, sell_book = books[buy_name], books[sell_name]
        buy_price = vwap_for_notional(buy_book.asks, leg)
        sell_price = vwap_for_notional(sell_book.bids, leg)
        if buy_price is None or sell_price is None:
            continue
        net, profit = mode2_economics(
            buy_price, sell_price, leg, fees.get(buy_name, 0.0), fees.get(sell_name, 0.0)
        )
        opportunities.append(
            Opportunity(
                symbol=buy_book.symbol,
                buy_exchange=buy_name,
                sell_exchange=sell_name,
                buy_price=buy_price,
                sell_price=sell_price,
                gross_pct=gross_spread_pct(buy_price, sell_price),
                net_pct=net,
                est_profit_brl=profit,
                mode=MODE_PREPOSITIONED,
            )
        )
    opportunities.sort(key=lambda o: o.net_pct, reverse=True)
    return opportunities


def compute_cycle(
    books: Mapping[str, Mapping[str, OrderBook]],
    usd_brl: UsdBrlRate | None,
    fees: Mapping[str, float],
    capital_brl: float,
    now: float | None = None,
) -> CycleSnapshot:
    """Consolida um ciclo completo: ágios por exchange e oportunidades.

    Somente rotas executáveis (comprar em uma exchange BR, vender em outra,
    capital pré-posicionado) entram em ``opportunities`` — o ágio vs.
    referência internacional não é lucro realizável (não dá para comprar
    USDT pela cotação do dólar comercial) e fica só nos ``premiums``.

    Args:
        books: mapa ``exchange -> símbolo -> order book`` do ciclo. Exchanges
            que falharam na coleta simplesmente não aparecem no mapa.
        usd_brl: cotação USD/BRL do ciclo (``None`` se a API falhou).
        fees: taxas taker por exchange, em fração.
        capital_brl: capital total; cada perna usa a metade como notional.
        now: timestamp do ciclo (default: ``time.time()``).
    """
    timestamp = time.time() if now is None else now

    binance_btc = books.get(BINANCE, {}).get("BTC/USDT")
    references: dict[str, ReferencePrice] = {}
    if (btc_ref := btc_reference_price(binance_btc, usd_brl, capital_brl)) is not None:
        references["BTC/BRL"] = btc_ref
    if (usdt_ref := usdt_reference_price(usd_brl)) is not None:
        references["USDT/BRL"] = usdt_ref

    premiums: list[ExchangePremium] = []
    opportunities: list[Opportunity] = []
    reference_leg = {"BTC/BRL": BINANCE, "USDT/BRL": REFERENCIA_USD}

    for symbol in _symbols_present(books):
        symbol_books = {
            exchange: books[exchange][symbol]
            for exchange in VENUES
            if symbol in books.get(exchange, {})
        }
        reference = references.get(symbol)
        ref_exchange = reference_leg.get(symbol, REFERENCIA_USD)
        ref_fee = fees.get(ref_exchange, 0.0)

        for book in symbol_books.values():
            premiums.append(
                exchange_premium(book, reference, fees.get(book.exchange, 0.0), ref_fee, capital_brl)
            )
        opportunities.extend(cross_exchange_opportunities(symbol_books, fees, capital_brl))

    opportunities.sort(key=lambda o: o.net_pct, reverse=True)
    return CycleSnapshot(
        timestamp=timestamp,
        premiums=premiums,
        opportunities=opportunities,
        references=references,
    )


def _symbols_present(books: Mapping[str, Mapping[str, OrderBook]]) -> list[str]:
    """Símbolos /BRL presentes em pelo menos uma venue, em ordem estável.

    Pares que não são contra BRL (ex.: BTC/USDT, usado só como referência)
    ficam de fora — não são rotas operáveis em reais.
    """
    seen: dict[str, None] = {}
    for exchange in VENUES:
        for symbol in books.get(exchange, {}):
            if symbol.endswith("/BRL"):
                seen.setdefault(symbol, None)
    return list(seen)
