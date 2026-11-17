#!/usr/bin/env bash
# Rotinas sem emissão. Usa o Python do host e a dependência GNRE isolada existente.
set -euo pipefail
umask 077
RAIZ=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
VERSAO=$(python3 -c 'import sys; print(str(sys.version_info.major)+str(sys.version_info.minor))')
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$RAIZ:$RAIZ/.runtime/python$VERSAO${PYTHONPATH:+:$PYTHONPATH}"
python3 -c 'import lxml.etree'
exec python3 "$RAIZ/operacao_gnre.py" "$@"
