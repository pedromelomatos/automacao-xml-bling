#!/usr/bin/env bash
# Prepara somente Redis e uma simulacao. Nao inicia downloads ou uploads.
set -euo pipefail
umask 077

RAIZ=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$RAIZ"

if [[ "$(uname -m)" != "x86_64" ]]; then
    echo 'Este pacote requer Debian/Linux x86_64 (amd64).' >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo 'Docker nao encontrado. Instale Docker Engine e Compose antes de continuar.' >&2
    exit 1
fi
if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
else
    command -v sudo >/dev/null 2>&1 || { echo 'Use uma conta com acesso ao Docker ou sudo.' >&2; exit 1; }
    DOCKER=(sudo docker)
    "${DOCKER[@]}" info >/dev/null
fi
dc() { "${DOCKER[@]}" compose "$@"; }
dc version

mkdir -p dados/entregas/metadados dados/logs segredos/rclone
chmod 700 segredos segredos/rclone
if [[ ! -e configuracao_dropbox.json ]]; then
    cp configuracao_dropbox.example.json configuracao_dropbox.json
fi
if [[ ! -e dados/entregas/ja_entregues.json ]]; then
    if [[ ! -f bootstrap/ja_entregues.json ]]; then
        echo 'Historico inicial ausente: forneca dados/entregas/ja_entregues.json ou use o pacote Debian com bootstrap.' >&2
        exit 1
    fi
    cp bootstrap/ja_entregues.json dados/entregas/ja_entregues.json
fi
for arquivo in .env segredos/tokens.json segredos/rclone/rclone.conf; do
    if [[ ! -s "$arquivo" ]]; then
        printf 'Credencial ausente: %s. Transfira o arquivo autorizado por SSH antes de continuar.\n' "$arquivo" >&2
        exit 1
    fi
    chmod 600 "$arquivo"
done

ATIVOS=$(dc ps --status running --services)
if grep -Eq '^(automacao-xml-bling|entregas-dropbox)$' <<< "$ATIVOS"; then
    echo 'Ja existe automacao/worker deste projeto ativo. Pare-o de forma planejada antes de reinstalar.' >&2
    exit 1
fi
dc build automacao-xml-bling
dc run --rm --no-deps entregas-dropbox python -c \
    "import json; c=json.load(open('/app/configuracao_dropbox.json')); assert c.get('habilitada') is False and c.get('modo')=='simulacao', 'A preparacao exige habilitada=false e modo=simulacao.'"
dc up -d --wait redis
dc run --rm --no-deps entregas-dropbox python integracao_dropbox.py simular \
    --saida /dados/logs/simulacao_instalacao.json

printf '\nPreparacao concluida: Redis ativo e simulacao salva em dados/logs/simulacao_instalacao.json.\n'
printf 'Os perfis download e entrega NAO foram iniciados. Nenhum upload foi autorizado por este instalador.\n'
printf 'Reconfira a cobertura e o corte de emissao antes de planejar qualquer ativacao.\n'
