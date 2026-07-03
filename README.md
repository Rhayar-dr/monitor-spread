# monitor-spread

Monitor de arbitragem de criptomoedas entre quatro venues que negociam
pares em reais: Mercado Bitcoin, Foxbit, Brasil Bitcoin e **Binance** (que
tem USDT/BRL direto, com taker bem menor — costuma ser a ponta que abre
rota). Por padrão monitora apenas **USDT/BRL** (referência: cotação
USD/BRL); o par **BTC/BRL** (referência: Binance BTC/USDT × USD/BRL) pode
ser reativado via `SYMBOLS` no `.env`.

A cada ciclo (default 10s) o monitor:

1. Coleta os order books públicos de todas as exchanges em paralelo
   ([collector.py](src/monitor_spread/collector.py));
2. Calcula o ágio de cada exchange BR frente à referência internacional e o
   spread líquido entre as próprias exchanges BR, já descontando taxas taker
   nas duas pontas ([calculator.py](src/monitor_spread/calculator.py));
3. Grava o snapshot no SQLite para análise futura
   ([storage.py](src/monitor_spread/storage.py));
4. Imprime um alerta destacado no console quando o spread líquido supera o
   threshold, com cooldown por rota ([alerter.py](src/monitor_spread/alerter.py)).

> ⚠️ **Somente leitura de mercado.** Este projeto consulta apenas endpoints
> públicos: nenhuma ordem é executada e nenhuma API key é necessária. Os
> alertas são informativos — sempre valide taxas, liquidez e custos de
> transferência (saque/depósito, PIX, rede) antes de operar de verdade.

## Setup

Requer Python 3.11+ e `make`.

```bash
make run        # cria .venv + .env na primeira vez e inicia o monitor
make dashboard  # interface web em http://localhost:8000 (em outro terminal)
make stop       # para monitor e dashboard (make restart = stop + run)
make report     # estatísticas do histórico (make report ARGS="--threshold 0.5")
make test       # testes unitários do calculator
make help       # lista todos os comandos (docker: make up / logs / down)
```

Na primeira execução o `.env` é criado a partir do `.env.example` —
**revise as taxas do seu tier em cada exchange** antes de confiar nos
alertas. Sem `make`, o equivalente manual:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
monitor-spread
```

Os alertas são impressos no próprio console do monitor (stdout), então em
Docker aparecem em `docker compose logs -f monitor`.

### Docker (VPS)

```bash
make up      # build + sobe em background (cria .env se faltar)
make logs    # acompanha os alertas
make down    # para o container
```

O banco fica persistido em `./data/spreads.db` no host. Para o relatório:

```bash
docker compose exec monitor monitor-spread-report --db /app/data/spreads.db
```

## A matemática do spread líquido

### Preço médio ponderado (VWAP), não topo do book

Comparar apenas o melhor bid/ask superestima o spread: para executar
R$ 11.000 (capital configurável em `CAPITAL_BRL`) normalmente é preciso
consumir vários níveis do book. Por isso todos os preços usados nos cálculos
são o **VWAP para o notional configurado**:

```
VWAP = notional / Σ quantidade_consumida_por_nível
```

caminhando pelos asks (para comprar) ou pelos bids (para vender) até
completar o notional. Se o book não tem liquidez suficiente (book raso), o
preço é considerado indisponível e a rota é descartada no ciclo.

### Referência internacional

O preço "justo" do BTC em BRL é derivado do mercado internacional:

```
ref_compra_BRL = VWAP_ask(Binance BTC/USDT) × ask(USD/BRL)
ref_venda_BRL  = VWAP_bid(Binance BTC/USDT) × bid(USD/BRL)
```

com USD/BRL vindo da [AwesomeAPI](https://docs.awesomeapi.com.br/api-de-moedas)
(dólar comercial). Para USDT/BRL a referência é a própria cotação USD/BRL
(aproximação USDT ≈ USD).

### Ágio vs. referência (termômetro, não lucro)

O **ágio bruto** de uma exchange BR é quanto o mercado local paga acima do
custo de comprar lá fora:

```
ágio_bruto % = (VWAP_bid_local / ref_compra_BRL − 1) × 100
```

Esse ágio é gravado no banco e serve de termômetro do mercado, mas **não
vira oportunidade nem alerta**: você não consegue comprar USDT pela cotação
do dólar comercial (câmbio real tem spread + IOF), então esse "lucro" não é
realizável. Alertas só disparam para rotas executáveis: comprar em uma
exchange BR e vender em outra.

### A estratégia: capital pré-posicionado

A estratégia é **sempre** operar com saldo nas duas pontas: BRL parado na
exchange onde se compra, USDT parado na exchange onde se vende. Quando a
janela abre, as duas ordens disparam simultaneamente — a janela só precisa
durar o tempo de duas chamadas de API. Não há transferência no caminho
crítico (transferir durante a operação viraria aposta direcional se o
spread fechasse no meio); a logística acontece depois, na velocidade que
ela quiser.

O spread líquido de cada rota desconta as corretagens taker das duas
pontas e é relativo ao **notional da operação**:

```
qty     = notional / (VWAP_ask_A × (1 + taker_A))
receita = qty × VWAP_bid_B × (1 − taker_B)
líquido % = (receita − notional) / notional × 100
```

O dataset grava as rotas com um notional padrão (metade do capital
configurado) para as séries ficarem comparáveis no tempo; o
dimensionamento pelo teu saldo real acontece no dashboard.

Trade-off assumido: o capital fica exposto ao risco das exchanges o tempo
todo — cripto em exchange não tem FGC.

### Rebalanceamento

Cada execução desloca o capital (BRL vai para a ponta de venda, USDT sai
de lá). Três formas de voltar à posição, da melhor para a pior:

1. **Janela inversa** (grátis): os spreads oscilam e em algum momento a
   direção contrária abre — a rota inversa lucra e, de brinde, devolve o
   inventário. Por isso rotas que reequilibram o capital usam threshold
   reduzido (`REVERSE_ALERT_THRESHOLD_PCT`, default 0%).
2. **Rebalanceamento pago**: transferir USDT via TRC-20 (~1 USDT; rede
   mais barata aceita por todas — o Mercado Bitcoin só aceita TRC-20 e
   ERC-20) e mover o BRL via PIX (grátis no MB/Foxbit; 0,5% + R$ 1,99 na
   Brasil Bitcoin). O custo desse ciclo completo é precificado por
   `route_economics` no calculator.
3. **Diluir em mais bolsos**: manter BRL e USDT em todas as exchanges e
   operar sempre no sentido que o saldo permite.

Se o relatório mostrar spreads oscilando nos dois sentidos, o
rebalanceamento pago praticamente some do modelo; se um lado for
sistematicamente mais caro, ele vira custo recorrente.

### Imposto de Renda

Vendas de cripto em exchanges nacionais até **R$ 35.000/mês** são isentas
de IR sobre o ganho; acima disso, o ganho do mês paga **15%** (regra
vigente em 2026 — a MP 1.303, que criaria alíquota única de 17,5%, caducou).
Com capital de R$ 11.000, a partir da 4ª execução no mês você ultrapassa a
isenção. Isso depende do agregado mensal, então não entra no lucro por
rota; o `monitor-spread-report` avisa quando o volume teórico de vendas de
um mês estoura a isenção.

O que o modelo ainda **não** desconta (avalie por fora): latência da
transferência TRC-20 (o spread pode fechar antes do USDT chegar — veja a
duração média das janelas no relatório) e slippage entre o snapshot e a
execução.

## Estrutura

```
src/monitor_spread/
├── config.py      # Settings (pydantic-settings, .env)
├── models.py      # dataclasses compartilhadas
├── collector.py   # coleta dos books (ccxt + httpx) e USD/BRL
├── calculator.py  # VWAP, ágio e spreads líquidos (puro, testado)
├── alerter.py     # alertas no console com cooldown
├── storage.py     # histórico em SQLite (aiosqlite)
├── main.py        # loop principal + graceful shutdown
├── report.py      # estatísticas do histórico (janelas, lucro teórico)
├── dashboard.py   # servidor web do dashboard (stdlib, lê o SQLite)
└── dashboard.html # interface (tema escuro, gráfico, auto-refresh 10s)
```

## Dashboard

`make dashboard` (ou o serviço `dashboard` no Docker, porta 8000) serve uma
interface web que lê o mesmo SQLite do monitor: melhores rotas por modo,
cotações e ágio por exchange, gráfico do spread líquido das últimas 6h com
a linha do threshold, e a tabela completa de rotas do último ciclo. A
página atualiza sozinha a cada 10s.

### Gestão de capital (o coração do dashboard)

O painel **"Meu capital"** guarda quanto você tem de BRL e USDT em cada
exchange — preencha os saldos reais na primeira vez (e edite quando
depositar/transferir por fora). A partir daí, tudo é dimensionado pelo teu
capital de verdade:

- **Executável por rota** = `min(BRL na ponta de compra, USDT na ponta de
  venda)` — a tabela mostra quanto dá para operar em cada rota e qual
  lucro isso renderia *no teu saldo*, não num capital hipotético.
- O banner no topo diz o que fazer agora:
  - **👋 Primeiro passo**: sem saldos cadastrados — preencha o painel.
  - **🎯 FAÇA AGORA** (verde): rota executável acima do threshold, com o
    tamanho exato que teu saldo permite. Rotas que também *reequilibram* o
    capital (movem BRL para a ponta que tem menos) disparam com o
    threshold reduzido.
  - **⚠️ Capital no lugar errado** (âmbar): há spread bom, mas falta BRL
    na ponta de compra ou USDT na de venda — ou transfira, ou aguarde a
    janela no sentido que teu saldo permite.
  - **Sem janela aberta**: melhor rota atual e quanto falta.
- Ao clicar em **"✅ Executei esta operação"**, o app faz o lançamento
  contábil: BRL sai da compra e vira USDT lá; USDT sai da venda e vira BRL
  lá. Depois de uma execução, a rota inversa naturalmente passa a ser a
  única com saldo — a recomendação se inverte sozinha. Há "desfazer última
  execução" para clique errado.

O app **não envia ordens**: por enquanto ele espelha o que você fizer.
Esse lançamento manual é deliberado — é o mesmo modelo de estado (saldos →
recomendação → execução → saldos) que a v2 automatizada vai operar, só que
com você no lugar do executor, para entender o funcionamento antes de
delegar.

## Relatório

`monitor-spread-report` agrupa registros consecutivos acima do threshold em
"janelas" de oportunidade e imprime: janelas por dia, duração média, spread
máximo, melhor horário do dia e o lucro teórico acumulado se todas as
janelas tivessem sido capturadas (uma execução por janela).

```bash
monitor-spread-report --db data/spreads.db --threshold 0.8
```
