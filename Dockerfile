FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# O histórico de spreads fica em /app/data (monte um volume aqui)
VOLUME ["/app/data"]

CMD ["monitor-spread"]
