"""Testes unitários da matemática de spreads (calculator.py).

Cobertura pedida: spread positivo, spread negativo, exchange fora do ar e
book raso. Os testes não dependem de rede nem de bibliotecas externas.
"""

from __future__ import annotations

import math

import pytest

from monitor_spread.calculator import (
    btc_reference_price,
    compute_cycle,
    cross_exchange_opportunities,
    route_economics,
    vwap_for_notional,
)
from monitor_spread.models import (
    BINANCE,
    BRASIL_BITCOIN,
    FOXBIT,
    MERCADO_BITCOIN,
    MODE_PREPOSITIONED,
    Level,
    OrderBook,
    UsdBrlRate,
)

CAPITAL = 11_000.0
FEES = {MERCADO_BITCOIN: 0.007, FOXBIT: 0.005, BRASIL_BITCOIN: 0.005, BINANCE: 0.001}
USD_BRL = UsdBrlRate(bid=5.40, ask=5.42, timestamp=0.0)


def make_book(exchange: str, symbol: str, bids: list[Level], asks: list[Level]) -> OrderBook:
    return OrderBook(exchange=exchange, symbol=symbol, bids=bids, asks=asks, timestamp=0.0)


def deep_book(exchange: str, symbol: str, mid: float, spread: float = 0.001) -> OrderBook:
    """Book com liquidez de sobra para o capital de teste."""
    bid = mid * (1 - spread)
    ask = mid * (1 + spread)
    depth = (CAPITAL * 5) / mid  # quantidade base suficiente em um único nível
    return make_book(exchange, symbol, [(bid, depth)], [(ask, depth)])


class TestVwap:
    def test_book_raso_retorna_none(self) -> None:
        """Book sem liquidez para o notional não deve produzir preço."""
        levels: list[Level] = [(600_000.0, 0.001)]  # só R$ 600 de liquidez
        assert vwap_for_notional(levels, CAPITAL) is None

    def test_book_vazio_e_notional_invalido(self) -> None:
        assert vwap_for_notional([], CAPITAL) is None
        assert vwap_for_notional([(100.0, 10.0)], 0.0) is None

    def test_vwap_pondera_multiplos_niveis(self) -> None:
        """Executar R$ 11.000 deve consumir dois níveis e ponderar o preço."""
        levels: list[Level] = [(100.0, 60.0), (110.0, 100.0)]
        # Nível 1: R$ 6.000 (60 un.); nível 2: R$ 5.000 (45,4545... un.)
        vwap = vwap_for_notional(levels, CAPITAL)
        assert vwap is not None
        expected_qty = 60.0 + 5_000.0 / 110.0
        assert math.isclose(vwap, CAPITAL / expected_qty, rel_tol=1e-9)
        # VWAP fica entre os dois níveis
        assert 100.0 < vwap < 110.0


class TestSpreadEntreExchangesBr:
    def test_spread_positivo(self) -> None:
        """Comprar na mais barata e vender na mais cara deve dar líquido > 0."""
        books = {
            FOXBIT: deep_book(FOXBIT, "BTC/BRL", 600_000.0),
            MERCADO_BITCOIN: deep_book(MERCADO_BITCOIN, "BTC/BRL", 615_000.0),
        }
        opportunities = cross_exchange_opportunities(books, FEES, CAPITAL)
        best = opportunities[0]
        assert best.buy_exchange == FOXBIT
        assert best.sell_exchange == MERCADO_BITCOIN
        assert best.mode == MODE_PREPOSITIONED
        assert best.gross_pct > best.net_pct > 0
        # Captura simultânea com o notional de uma perna (metade do capital)
        leg = CAPITAL / 2
        qty = leg / (best.buy_price * (1 + FEES[FOXBIT]))
        expected_profit = qty * best.sell_price * (1 - FEES[MERCADO_BITCOIN]) - leg
        assert math.isclose(best.est_profit_brl, expected_profit, rel_tol=1e-9)
        assert math.isclose(best.net_pct, expected_profit / leg * 100.0, rel_tol=1e-9)

    def test_spread_negativo(self) -> None:
        """Preços praticamente iguais: taxas tornam todas as rotas negativas."""
        books = {
            FOXBIT: deep_book(FOXBIT, "BTC/BRL", 600_000.0),
            MERCADO_BITCOIN: deep_book(MERCADO_BITCOIN, "BTC/BRL", 600_100.0),
        }
        opportunities = cross_exchange_opportunities(books, FEES, CAPITAL)
        assert opportunities  # rotas existem, mas nenhuma é lucrativa
        assert all(o.net_pct < 0 for o in opportunities)

    def test_book_raso_e_ignorado(self) -> None:
        """Exchange com book raso não deve gerar rota (nem quebrar o cálculo)."""
        books = {
            FOXBIT: deep_book(FOXBIT, "BTC/BRL", 600_000.0),
            BRASIL_BITCOIN: make_book(
                BRASIL_BITCOIN, "BTC/BRL", [(614_000.0, 0.001)], [(616_000.0, 0.001)]
            ),
        }
        opportunities = cross_exchange_opportunities(books, FEES, CAPITAL)
        assert opportunities == []


class TestComputeCycle:
    def _books(self) -> dict[str, dict[str, OrderBook]]:
        return {
            FOXBIT: {"BTC/BRL": deep_book(FOXBIT, "BTC/BRL", 600_000.0)},
            MERCADO_BITCOIN: {"BTC/BRL": deep_book(MERCADO_BITCOIN, "BTC/BRL", 615_000.0)},
            BINANCE: {"BTC/USDT": deep_book(BINANCE, "BTC/USDT", 110_000.0)},
        }

    def test_agio_positivo_vs_referencia(self) -> None:
        """Exchange BR mais cara que a referência deve ter ágio positivo."""
        snapshot = compute_cycle(self._books(), USD_BRL, FEES, CAPITAL, now=0.0)
        # Referência ~ 110.000 * 5,42 = R$ 596.200; Mercado Bitcoin a 615.000
        premium = next(p for p in snapshot.premiums if p.exchange == MERCADO_BITCOIN)
        assert premium.agio_bruto_pct is not None and premium.agio_bruto_pct > 0
        assert premium.agio_liquido_pct is not None
        assert premium.agio_liquido_pct < premium.agio_bruto_pct
        # A melhor oportunidade do ciclo deve estar ordenada primeiro
        assert snapshot.opportunities[0].net_pct == max(o.net_pct for o in snapshot.opportunities)

    def test_exchange_fora_do_ar(self) -> None:
        """Sem a Binance e sem uma BR, o ciclo segue com o que sobrou."""
        books = self._books()
        del books[BINANCE]
        del books[MERCADO_BITCOIN]
        snapshot = compute_cycle(books, USD_BRL, FEES, CAPITAL, now=0.0)
        # Sem referência internacional de BTC, ágio fica indefinido
        premium = next(p for p in snapshot.premiums if p.exchange == FOXBIT)
        assert premium.agio_bruto_pct is None
        assert premium.agio_liquido_pct is None
        assert premium.bid_vwap is not None  # cotação local continua disponível
        # Só uma exchange BR: nenhuma rota BR<->BR possível
        assert all(o.buy_exchange != o.sell_exchange for o in snapshot.opportunities)
        assert not any(
            {o.buy_exchange, o.sell_exchange} <= {FOXBIT, MERCADO_BITCOIN, BRASIL_BITCOIN}
            for o in snapshot.opportunities
        )

    def test_usd_brl_indisponivel(self) -> None:
        """Sem USD/BRL não há referência nenhuma, mas o ciclo não quebra."""
        snapshot = compute_cycle(self._books(), None, FEES, CAPITAL, now=0.0)
        assert snapshot.references == {}
        assert all(p.agio_bruto_pct is None for p in snapshot.premiums)
        # Rotas BR<->BR continuam sendo calculadas normalmente
        assert any(o.buy_exchange == FOXBIT and o.sell_exchange == MERCADO_BITCOIN
                   for o in snapshot.opportunities)


class TestRotasEReferencia:
    def _books(self) -> dict[str, dict[str, OrderBook]]:
        return {
            FOXBIT: {"USDT/BRL": deep_book(FOXBIT, "USDT/BRL", 5.20)},
            MERCADO_BITCOIN: {"USDT/BRL": deep_book(MERCADO_BITCOIN, "USDT/BRL", 5.30)},
        }

    def test_rotas_nao_executaveis_ficam_fora(self) -> None:
        """Ágio vs. referência não vira oportunidade: só rotas BR -> BR."""
        snapshot = compute_cycle(self._books(), USD_BRL, FEES, CAPITAL, now=0.0)
        exchanges_reais = {FOXBIT, MERCADO_BITCOIN, BRASIL_BITCOIN}
        assert snapshot.opportunities  # rotas BR<->BR existem
        for o in snapshot.opportunities:
            assert o.buy_exchange in exchanges_reais
            assert o.sell_exchange in exchanges_reais
            assert o.mode == MODE_PREPOSITIONED  # estratégia única
        # O ágio continua disponível como termômetro nos premiums
        assert any(p.agio_bruto_pct is not None for p in snapshot.premiums)


class TestRebalanceamentoPago:
    """route_economics precifica o rebalanceamento ativo (TRC-20 + PIX)."""

    def test_ciclo_completo_desconta_tudo(self) -> None:
        result = route_economics(
            5.20, 5.30, CAPITAL, 0.005, 0.007,
            transfer_fee_base=1.0, cashout_pct=0.005, cashout_fixed_brl=1.99,
        )
        assert result is not None
        net, profit = result
        qty = CAPITAL / (5.20 * 1.005) - 1.0
        receita = qty * 5.30 * (1 - 0.007) * (1 - 0.005) - 1.99
        assert math.isclose(profit, receita - CAPITAL, rel_tol=1e-9)
        assert math.isclose(net, profit / CAPITAL * 100.0, rel_tol=1e-9)

    def test_transferencia_engole_notional_pequeno(self) -> None:
        """Se a taxa de transferência consome toda a quantidade, sem rota."""
        assert route_economics(5.20, 5.30, 5.0, 0.005, 0.007, transfer_fee_base=1.0) is None


class TestCapturaPrePosicionada:
    def test_book_com_liquidez_para_a_perna_gera_rota(self) -> None:
        """O VWAP usa o notional da perna: R$ 6.360 de book bastam."""
        meio_book = make_book(
            MERCADO_BITCOIN,
            "USDT/BRL",
            bids=[(5.30, 1_200.0)],  # ~R$ 6.360: cabe a perna de R$ 5.500
            asks=[(5.31, 1_200.0)],
        )
        books = {
            FOXBIT: {"USDT/BRL": deep_book(FOXBIT, "USDT/BRL", 5.20)},
            MERCADO_BITCOIN: {"USDT/BRL": meio_book},
        }
        snapshot = compute_cycle(books, None, FEES, CAPITAL, now=0.0)
        assert any(
            o.buy_exchange == FOXBIT and o.sell_exchange == MERCADO_BITCOIN
            for o in snapshot.opportunities
        )

    def test_net_pct_e_relativo_ao_notional(self) -> None:
        """Sanidade da fórmula: net% coerente com o fluxo de caixa da perna."""
        books = {
            FOXBIT: {"USDT/BRL": deep_book(FOXBIT, "USDT/BRL", 5.20)},
            MERCADO_BITCOIN: {"USDT/BRL": deep_book(MERCADO_BITCOIN, "USDT/BRL", 5.30)},
        }
        snapshot = compute_cycle(books, None, FEES, CAPITAL, now=0.0)
        rota = next(
            o for o in snapshot.opportunities
            if o.buy_exchange == FOXBIT and o.sell_exchange == MERCADO_BITCOIN
        )
        leg = CAPITAL / 2
        assert math.isclose(rota.est_profit_brl, leg * rota.net_pct / 100.0, rel_tol=1e-9)


class TestReferencia:
    def test_referencia_btc_brl(self) -> None:
        book = deep_book(BINANCE, "BTC/USDT", 110_000.0)
        ref = btc_reference_price(book, USD_BRL, CAPITAL)
        assert ref is not None
        assert math.isclose(ref.buy_brl, 110_000.0 * 1.001 * 5.42, rel_tol=1e-9)
        assert ref.sell_brl < ref.buy_brl

    @pytest.mark.parametrize("book,rate", [(None, USD_BRL), (deep_book(BINANCE, "BTC/USDT", 110_000.0), None)])
    def test_referencia_indisponivel(self, book: OrderBook | None, rate: UsdBrlRate | None) -> None:
        assert btc_reference_price(book, rate, CAPITAL) is None
