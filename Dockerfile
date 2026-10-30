FROM python:3.14.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo \
    AUTOMACAO_DATA_DIR=/dados \
    BLING_TOKENS_FILE=/segredos/tokens.json \
    AUTOMACAO_DROPBOX_CONFIG=/app/configuracao_dropbox.json \
    RCLONE_CONFIG=/segredos/rclone/rclone.conf

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
COPY dependencias_docker/ /wheels/
COPY instalar_rclone.py ./
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && python instalar_rclone.py --cache /wheels/rclone --destino /usr/local/bin/rclone \
    && rm -rf /wheels

COPY baixar_xmls.py bling_auth.py organizar_xmls.py executar_diario.py agendador_docker.py preparar_instalacao.py ./
COPY integracao_dropbox.py registro_entregas.py transporte_dropbox.py ./
COPY configuracao.json ./
COPY configuracao_dropbox.example.json ./configuracao_dropbox.json

RUN mkdir -p /dados /segredos/rclone

CMD ["python", "agendador_docker.py"]
