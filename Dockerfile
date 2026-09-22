FROM python:3.14.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo \
    AUTOMACAO_DATA_DIR=/dados \
    BLING_TOKENS_FILE=/segredos/tokens.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
COPY dependencias_docker/ /wheels/
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY baixar_xmls.py bling_auth.py organizar_xmls.py executar_diario.py agendador_docker.py ./
COPY configuracao.json ./

RUN mkdir -p /dados /segredos

CMD ["python", "agendador_docker.py"]
