"""Instala rclone Linux amd64 de release fixo, após conferir SHA-256 oficial."""

import argparse
import hashlib
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen


VERSAO_RCLONE = "v1.75.2"
NOME_ARQUIVO = f"rclone-{VERSAO_RCLONE}-linux-amd64.zip"
URL_RELEASE = f"https://github.com/rclone/rclone/releases/download/{VERSAO_RCLONE}/"
MEMBRO_BINARIO = f"rclone-{VERSAO_RCLONE}-linux-amd64/rclone"


def obter_url(url):
    requisicao = Request(url, headers={"User-Agent": "automacao-xml-bling-instalacao"})
    with urlopen(requisicao, timeout=90) as resposta:
        return resposta.read()


def hash_oficial(conteudo):
    for linha in conteudo.decode("utf-8").splitlines():
        partes = linha.split()
        if len(partes) >= 2 and partes[-1].lstrip("*") == NOME_ARQUIVO:
            digest = partes[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    raise RuntimeError("SHA-256 oficial do rclone Linux não encontrado.")


def verificar_arquivo(arquivo, checksums):
    esperado = hash_oficial(Path(checksums).read_bytes())
    atual = hashlib.sha256(Path(arquivo).read_bytes()).hexdigest()
    if atual != esperado:
        raise RuntimeError("SHA-256 divergente no rclone; instalação interrompida.")
    return atual


def preparar_cache(cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    checksums = cache / "SHA256SUMS"
    arquivo = cache / NOME_ARQUIVO
    if not checksums.is_file():
        checksums.write_bytes(obter_url(URL_RELEASE + "SHA256SUMS"))
    if not arquivo.is_file():
        conteudo = obter_url(URL_RELEASE + NOME_ARQUIVO)
        with tempfile.NamedTemporaryFile(dir=cache, prefix="rclone-", suffix=".tmp", delete=False) as temporario:
            caminho_temporario = Path(temporario.name)
            temporario.write(conteudo)
        try:
            verificar_arquivo(caminho_temporario, checksums)
            caminho_temporario.replace(arquivo)
        finally:
            caminho_temporario.unlink(missing_ok=True)
    verificar_arquivo(arquivo, checksums)
    return arquivo, checksums


def instalar(cache, destino):
    arquivo, checksums = preparar_cache(cache)
    verificar_arquivo(arquivo, checksums)
    with zipfile.ZipFile(arquivo) as pacote:
        conteudo = pacote.read(MEMBRO_BINARIO)
    if not conteudo.startswith(b"\x7fELF"):
        raise RuntimeError("O arquivo verificado não contém o binário Linux esperado.")
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_name(destino.name + f".{os.getpid()}.tmp")
    try:
        temporario.write_bytes(conteudo)
        temporario.chmod(0o755)
        temporario.replace(destino)
    finally:
        temporario.unlink(missing_ok=True)
    return destino


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("tmp/ferramentas/rclone-linux"))
    parser.add_argument("--destino", type=Path)
    args = parser.parse_args()
    try:
        if args.destino:
            print(f"rclone {VERSAO_RCLONE} verificado e instalado em {instalar(args.cache, args.destino)}")
        else:
            arquivo, _ = preparar_cache(args.cache)
            print(f"rclone {VERSAO_RCLONE} verificado no cache: {arquivo}")
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile) as erro:
        raise SystemExit(f"Falha ao preparar rclone: {type(erro).__name__}.")


if __name__ == "__main__":
    main()
