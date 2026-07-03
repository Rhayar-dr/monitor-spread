# Atalhos do monitor-spread. Rode `make` (ou `make help`) para ver os alvos.

.DEFAULT_GOAL := help
PYTHON ?= python3.11
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: help install env run dashboard stop restart report test up down logs rebuild clean

help: ## Lista os comandos disponíveis
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

$(VENV):
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q -e ".[dev]"

install: $(VENV) ## Cria o .venv e instala as dependências

env: ## Cria o .env a partir do .env.example (se não existir)
	@test -f .env || (cp .env.example .env && echo ".env criado — revise as taxas antes de rodar!")

run: install env ## Roda o monitor no terminal (Ctrl+C para parar)
	$(BIN)/monitor-spread

dashboard: install env ## Sobe o dashboard web em http://localhost:8000
	$(BIN)/monitor-spread-dashboard

stop: ## Para o monitor e o dashboard locais (SIGTERM = shutdown gracioso)
	-@pkill -f "[m]onitor-spread-dashboard" && echo "dashboard parado" || echo "dashboard já estava parado"
	-@pkill -f "[m]onitor-spread$$" && echo "monitor parado" || echo "monitor já estava parado"

restart: stop run ## Para tudo e sobe o monitor de novo (dashboard: make dashboard)

report: install ## Estatísticas do histórico (ex.: make report ARGS="--threshold 0.5")
	$(BIN)/monitor-spread-report --db data/spreads.db $(ARGS)

test: install ## Roda os testes unitários
	$(BIN)/python -m pytest -q

up: env ## Sobe o monitor em Docker (background)
	docker compose up -d --build

down: ## Para o monitor em Docker
	docker compose down

logs: ## Acompanha os logs do container (é onde saem os alertas)
	docker compose logs -f monitor

rebuild: env ## Reconstrói a imagem e reinicia o container
	docker compose up -d --build --force-recreate

clean: ## Remove .venv e caches (preserva o banco em data/)
	rm -rf $(VENV) .pytest_cache src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
