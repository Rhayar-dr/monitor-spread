"""Coleta de order books públicos e da cotação USD/BRL.

Mercado Bitcoin e Binance são acessadas via ccxt (async). Foxbit e Brasil
Bitcoin não têm cobertura estável no ccxt, então usam a API REST pública
diretamente via httpx. Nenhuma credencial é necessária: apenas endpoints
públicos de mercado são consultados.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import ccxt.async_support as ccxt
import httpx

from .config import Settings
from .models import (
    BINANCE,
    BRASIL_BITCOIN,
    FOXBIT,
    MERCADO_BITCOIN,
    Level,
    OrderBook,
    UsdBrlRate,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

USD_BRL_URL = "https://economia.awesomeapi.com.br/json/last/USD-BRL"
FOXBIT_BASE_URL = "https://api.foxbit.com.br/rest/v3"
BRASIL_BITCOIN_BASE_URL = "https://brasilbitcoin.com.br/API"
BOOK_DEPTH = 50


def _parse_level(item: Any) -> Level | None:
    """Converte um nível de book em (preço, quantidade), tolerante a formatos.

    Aceita listas ``[preço, quantidade, ...]`` e dicionários com chaves em
    português ou inglês (``preco``/``price``, ``quantidade``/``amount``).
    """
    try:
        if isinstance(item, dict):
            price = item.get("preco", item.get("price"))
            amount = item.get("quantidade", item.get("amount", item.get("quantity")))
        else:
            price, amount = item[0], item[1]
        if price is None or amount is None:
            return None
        return float(price), float(amount)
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _parse_levels(raw: Any, descending: bool) -> list[Level]:
    """Normaliza e ordena uma lista de níveis de book."""
    if not isinstance(raw, list):
        return []
    levels = [level for item in raw if (level := _parse_level(item)) is not None]
    levels.sort(key=lambda lv: lv[0], reverse=descending)
    return levels


class BaseCollector(ABC):
    """Interface comum dos coletores de order book."""

    name: str
    symbols: tuple[str, ...]

    @abstractmethod
    async def fetch_order_book(self, symbol: str) -> OrderBook:
        """Busca o order book público (topo + profundidade) de um símbolo."""

    async def close(self) -> None:
        """Libera recursos de rede do coletor."""


class CcxtCollector(BaseCollector):
    """Coletor genérico para exchanges cobertas pelo ccxt (async)."""

    def __init__(self, name: str, ccxt_id: str, symbols: tuple[str, ...], timeout_seconds: float) -> None:
        self.name = name
        self.symbols = symbols
        exchange_class = getattr(ccxt, ccxt_id)
        self._client = exchange_class({"timeout": int(timeout_seconds * 1000), "enableRateLimit": True})

    async def fetch_order_book(self, symbol: str) -> OrderBook:
        raw = await self._client.fetch_order_book(symbol, limit=BOOK_DEPTH)
        return OrderBook(
            exchange=self.name,
            symbol=symbol,
            bids=_parse_levels(raw.get("bids", []), descending=True),
            asks=_parse_levels(raw.get("asks", []), descending=False),
            timestamp=time.time(),
        )

    async def close(self) -> None:
        await self._client.close()


class FoxbitCollector(BaseCollector):
    """Coletor da API REST v3 pública da Foxbit."""

    name = FOXBIT

    def __init__(self, http: httpx.AsyncClient, symbols: tuple[str, ...]) -> None:
        self._http = http
        self.symbols = symbols

    async def fetch_order_book(self, symbol: str) -> OrderBook:
        market_id = symbol.replace("/", "").lower()  # BTC/BRL -> btcbrl
        response = await self._http.get(
            f"{FOXBIT_BASE_URL}/markets/{market_id}/orderbook",
            params={"depth": BOOK_DEPTH},
        )
        response.raise_for_status()
        data = response.json()
        return OrderBook(
            exchange=self.name,
            symbol=symbol,
            bids=_parse_levels(data.get("bids", []), descending=True),
            asks=_parse_levels(data.get("asks", []), descending=False),
            timestamp=time.time(),
        )


class BrasilBitcoinCollector(BaseCollector):
    """Coletor da API pública da Brasil Bitcoin (books sempre cotados em BRL)."""

    name = BRASIL_BITCOIN

    def __init__(self, http: httpx.AsyncClient, symbols: tuple[str, ...]) -> None:
        self._http = http
        self.symbols = symbols

    async def fetch_order_book(self, symbol: str) -> OrderBook:
        coin = symbol.split("/", 1)[0]  # BTC/BRL -> BTC
        response = await self._http.get(f"{BRASIL_BITCOIN_BASE_URL}/orderbook/{coin}")
        response.raise_for_status()
        data = response.json()
        return OrderBook(
            exchange=self.name,
            symbol=symbol,
            bids=_parse_levels(data.get("buy", []), descending=True),
            asks=_parse_levels(data.get("sell", []), descending=False),
            timestamp=time.time(),
        )


class MarketDataCollector:
    """Orquestra a coleta de todos os books e da cotação USD/BRL de um ciclo.

    Cada requisição roda com timeout e retry exponencial; falhas individuais
    são logadas e não derrubam o ciclo — a exchange fica de fora do snapshot.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        symbols = settings.symbol_list()
        self._collectors: list[BaseCollector] = [
            CcxtCollector(MERCADO_BITCOIN, "mercado", symbols, settings.request_timeout_seconds),
            FoxbitCollector(self._http, symbols),
            BrasilBitcoinCollector(self._http, symbols),
        ]
        # A Binance só é necessária como referência internacional do BTC;
        # para USDT a referência é a própria cotação USD/BRL.
        if "BTC/BRL" in symbols:
            self._collectors.append(
                CcxtCollector(BINANCE, "binance", ("BTC/USDT",), settings.request_timeout_seconds)
            )

    async def _with_retry(self, description: str, factory: Callable[[], Awaitable[T]]) -> T:
        """Executa uma corrotina com timeout e retry com backoff exponencial."""
        attempts = max(1, self._settings.retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(factory(), self._settings.request_timeout_seconds)
            except Exception as exc:
                if attempt == attempts:
                    raise
                delay = self._settings.retry_base_delay_seconds * 2 ** (attempt - 1)
                logger.warning(
                    "falha em %s (tentativa %d/%d): %s — nova tentativa em %.1fs",
                    description, attempt, attempts, exc, delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def fetch_usd_brl(self) -> UsdBrlRate:
        """Busca a cotação USD/BRL comercial na AwesomeAPI."""
        response = await self._http.get(USD_BRL_URL)
        response.raise_for_status()
        quote = response.json()["USDBRL"]
        return UsdBrlRate(bid=float(quote["bid"]), ask=float(quote["ask"]), timestamp=time.time())

    async def collect(self) -> tuple[dict[str, dict[str, OrderBook]], UsdBrlRate | None]:
        """Coleta todos os books e a cotação USD/BRL em paralelo.

        Returns:
            Mapa ``exchange -> símbolo -> book`` apenas com as coletas que
            deram certo, e a cotação USD/BRL (``None`` se indisponível).
        """
        jobs: list[tuple[str, str, Awaitable[OrderBook]]] = []
        for collector in self._collectors:
            for symbol in collector.symbols:
                description = f"{collector.name} {symbol}"
                jobs.append(
                    (
                        collector.name,
                        symbol,
                        self._with_retry(description, lambda c=collector, s=symbol: c.fetch_order_book(s)),
                    )
                )
        usd_task = self._with_retry("USD/BRL", self.fetch_usd_brl)

        results = await asyncio.gather(*(job[2] for job in jobs), usd_task, return_exceptions=True)

        books: dict[str, dict[str, OrderBook]] = {}
        for (exchange, symbol, _), result in zip(jobs, results[:-1]):
            if isinstance(result, BaseException):
                logger.error("exchange=%s symbol=%s coleta falhou: %s", exchange, symbol, result)
                continue
            books.setdefault(exchange, {})[symbol] = result

        usd_result = results[-1]
        usd_brl: UsdBrlRate | None
        if isinstance(usd_result, BaseException):
            logger.error("cotação USD/BRL indisponível: %s", usd_result)
            usd_brl = None
        else:
            usd_brl = usd_result
        return books, usd_brl

    async def close(self) -> None:
        """Fecha os clientes de rede (ccxt e httpx)."""
        for collector in self._collectors:
            try:
                await collector.close()
            except Exception:  # pragma: no cover - melhor esforço no shutdown
                logger.debug("erro ao fechar coletor %s", collector.name, exc_info=True)
        await self._http.aclose()
