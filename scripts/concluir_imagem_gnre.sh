#!/usr/bin/env bash
# Constrói e valida uma imagem candidata, sem volumes fiscais ou emissão de guias.
# Execute com o usuário habitual; somente os comandos Docker podem pedir sudo.
set -euo pipefail
umask 077
RAIZ=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$RAIZ"
if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
else
    DOCKER=(sudo docker)
    "${DOCKER[@]}" info >/dev/null
fi

MARCA=$(date -u +%Y%m%d-%H%M%S)-$$
CANDIDATA="automacao-gnre:validacao-$MARCA"
ANTERIOR="automacao-gnre:antes-operacao-$MARCA"
"${DOCKER[@]}" image inspect automacao-xml-bling:2.0-dropbox >/dev/null
"${DOCKER[@]}" build --file Dockerfile.gnre --tag "$CANDIDATA" .

# Ambiente equivalente ao serviço: usuário comum, raiz somente leitura e /tmp
# descartável. Sem rede, sem montar dados, certificados ou credenciais do host.
"${DOCKER[@]}" run --rm -i --network none --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 \
    --user "$(id -u):$(id -g)" --cap-drop ALL \
    --security-opt no-new-privileges:true --entrypoint python "$CANDIDATA" - <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess

import lxml.etree
import emitir_gnre
import operacao_gnre
from conferir_pagamento_gnre import conferir_codigo, extrair_texto

linha = '858000000003016800980006000002611201423932722002'
codigo = ''.join(linha[i:i + 11] for i in range(0, 48, 12))
assert conferir_codigo(codigo, linha, '1.68')['total'] == '1.68'

# PDF sintético, sem dados fiscais, para provar que Poppler funciona com /tmp.
stream = b'BT /F1 12 Tf 20 80 Td (Teste interno GNRE) Tj ET'
objetos = [b'<< /Type /Catalog /Pages 2 0 R >>',
           b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
           b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
           b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
           b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
pdf, offsets = b'%PDF-1.4\n', [0]
for numero, objeto in enumerate(objetos, 1):
    offsets.append(len(pdf))
    pdf += str(numero).encode() + b' 0 obj\n' + objeto + b'\nendobj\n'
xref = len(pdf)
pdf += b'xref\n0 6\n0000000000 65535 f \n'
pdf += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
pdf += b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'
assert 'Teste interno GNRE' in extrair_texto(pdf)
subprocess.run(['python', '/app/operacao_gnre.py', '--help'], check=True)
nomes = ['consultar_gnre.py', 'emitir_gnre.py', 'conferir_retorno_gnre.py',
         'controle_producao_gnre.py', 'conferir_pagamento_gnre.py', 'operacao_gnre.py']
print('GNRE_DOCKER_VALIDADO ' + json.dumps({n: hashlib.sha256(Path('/app', n).read_bytes()).hexdigest()
                                        for n in nomes}, sort_keys=True))
PY

# A referência usada pela operação só muda depois da validação completa.
if "${DOCKER[@]}" image inspect automacao-gnre:0.1 >/dev/null 2>&1; then
    "${DOCKER[@]}" image tag automacao-gnre:0.1 "$ANTERIOR"
    printf 'Imagem anterior preservada: %s\n' "$ANTERIOR"
fi
"${DOCKER[@]}" image tag "$CANDIDATA" automacao-gnre:0.1
printf 'Imagem atualizada e validada: automacao-gnre:0.1\n'
printf 'Nenhum lote transmitido; controles e bloqueio de producao preservados.\n'
