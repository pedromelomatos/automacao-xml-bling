#!/usr/bin/env bash
# Executa o fluxo GNRE no contêiner com resultados persistentes no Debian.
set -euo pipefail
umask 077

RAIZ=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CERT_DIR="$HOME/.local/share/automacao-xml-bling/gnre"
CERT_ARQUIVO="$CERT_DIR/identidade.pem"

if [[ ! -f "$CERT_ARQUIVO" || -L "$CERT_DIR" || -L "$CERT_ARQUIVO" ]]; then
    echo 'Credencial GNRE ausente ou caminho inválido.' >&2
    exit 1
fi
if [[ "$(stat -c %u "$CERT_ARQUIVO")" != "$(id -u)" ||
      "$(stat -c %u "$CERT_DIR")" != "$(id -u)" ||
      "$(stat -c %a "$CERT_ARQUIVO")" != 600 ||
      "$(stat -c %a "$CERT_DIR")" != 700 ]]; then
    echo 'Use o proprietário do certificado, com pasta 700 e arquivo 600.' >&2
    exit 1
fi
mkdir -p "$RAIZ/entrada" "$RAIZ/dados-gnre"
export GNRE_UID="$(id -u)"
export GNRE_GID="$(id -g)"
export GNRE_CERT_DIR="$CERT_DIR"

if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
else
    DOCKER=(sudo env "GNRE_UID=$GNRE_UID" "GNRE_GID=$GNRE_GID" "GNRE_CERT_DIR=$GNRE_CERT_DIR" docker)
    "${DOCKER[@]}" info >/dev/null
fi

dc() { "${DOCKER[@]}" compose --env-file /dev/null -f "$RAIZ/docker-compose.gnre.yml" "$@"; }
dc build gnre-emissao
printf 'Pasta de resultados no Debian: %s/dados-gnre\n' "$RAIZ"
dc run --rm --no-deps gnre-emissao python /app/emitir_gnre.py "$@"
