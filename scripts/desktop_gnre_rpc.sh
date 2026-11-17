#!/usr/bin/env bash
# Único comando permitido às chaves do desktop. Não interpreta comando remoto.
set -euo pipefail
umask 077
RAIZ=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
VERSAO=$(python3 -c 'import sys; print(str(sys.version_info.major)+str(sys.version_info.minor))')
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$RAIZ:$RAIZ/.runtime/python$VERSAO"
exec python3 "$RAIZ/desktop_gnre_servidor.py" "$@"
