"""Gera o pacote Debian em simulação, sem credenciais, XMLs ou estados privados."""

import argparse
import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from instalar_rclone import NOME_ARQUIVO, preparar_cache


BASE_DIR = Path(__file__).resolve().parent
ARQUIVOS = (
    "Dockerfile", "docker-compose.yml", ".dockerignore", "requirements.txt", ".env.example",
    "baixar_xmls.py", "bling_auth.py", "organizar_xmls.py", "executar_diario.py",
    "agendador_docker.py", "preparar_instalacao.py", "integracao_dropbox.py",
    "registro_entregas.py", "transporte_dropbox.py", "instalar_rclone.py",
    "configuracao_dropbox.example.json", "docs/diagnostico_debian.sh",
    "docs/instalar_integracao_debian.sh", "docs/integracao_debian.md",
)
HISTORICO_PADRAO = "dados/integracao_dropbox/preparacao_ativacao_2026-10-29.json"


def preparar_historico(caminho):
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    notas = dados.get("notas", dados.get("ja_entregues_no_recorte_28_29"))
    if not isinstance(notas, list):
        raise RuntimeError("Forneça um histórico explícito de notas já entregues.")
    resultado = []
    for nota in notas:
        chave = str(nota.get("chave", ""))
        cnpj = str(nota.get("cnpj_emitente", ""))
        canal = str(nota.get("canal_bling", ""))
        if not re.fullmatch(r"\d{44}", chave) or cnpj != chave[6:20] or not canal.isdecimal():
            raise RuntimeError("Histórico inicial contém chave, emitente ou canal inválido.")
        # Lista explícita: não copiar campos privados nem dados de destinatários.
        resultado.append({"chave": chave, "cnpj_emitente": cnpj, "canal_bling": canal})
    if len({n["chave"] for n in resultado}) != len(resultado):
        raise RuntimeError("Histórico inicial contém chaves repetidas.")
    return {"origem": "Fotografia de entregas conferidas; revalidar cobertura antes de ativar.",
            "notas": sorted(resultado, key=lambda n: n["chave"])}


def gerar_pacote(raiz=BASE_DIR, destino=None, historico=None, cache_rclone=None):
    raiz = Path(raiz).resolve()
    destino = Path(destino or raiz / "dist/Pacote-Integracao-Dropbox-Debian.zip").resolve()
    historico = Path(historico or raiz / HISTORICO_PADRAO)
    cache_rclone = Path(cache_rclone or raiz / "tmp/ferramentas/rclone-linux")
    ausentes = [nome for nome in ARQUIVOS if not (raiz / nome).is_file()]
    if ausentes:
        raise RuntimeError("Arquivos ausentes: " + ", ".join(ausentes))
    config_unidades = raiz / "configuracao.json"
    if not config_unidades.is_file():
        config_unidades = raiz / "configuracao.example.json"
    if not config_unidades.is_file() or not historico.is_file():
        raise RuntimeError("Configuração local ou histórico inicial ausente.")
    wheels = sorted((raiz / "dependencias_docker").glob("*.whl"))
    if not wheels or not any(p.name.startswith("redis-") for p in wheels):
        raise RuntimeError("Dependências Linux incompletas: inclua o wheel redis-py.")
    seed = preparar_historico(historico)
    cfg = json.loads((raiz / "configuracao_dropbox.example.json").read_text(encoding="utf-8"))
    cfg.update(habilitada=False, modo="simulacao")
    arquivo_rclone, checksums = preparar_cache(cache_rclone)
    conteudos = {nome: (raiz / nome).read_bytes() for nome in ARQUIVOS}
    for nome in conteudos:
        if nome.endswith(".sh"):
            conteudos[nome] = conteudos[nome].replace(b"\r\n", b"\n")
    conteudos["configuracao.json"] = config_unidades.read_bytes()
    conteudos["configuracao_dropbox.example.json"] = (json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    conteudos["bootstrap/ja_entregues.json"] = (json.dumps(seed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    conteudos["LEIA-ME.txt"] = (
        "INTEGRACAO DROPBOX - DEBIAN - SOMENTE SIMULACAO\n"
        "Leia docs/integracao_debian.md antes de instalar.\n"
        "Credenciais .env, tokens.json e rclone.conf devem ser transferidas separadamente.\n"
        "Execute bash docs/instalar_integracao_debian.sh no Debian.\n"
        "O instalador sobe apenas Redis e uma simulacao; nao inicia downloads ou uploads.\n"
        "O bootstrap e uma fotografia antiga: reaudite cobertura e corte antes de ativar.\n"
    ).encode("utf-8")
    for wheel in wheels:
        conteudos["dependencias_docker/" + wheel.name] = wheel.read_bytes()
    conteudos["dependencias_docker/rclone/" + NOME_ARQUIVO] = arquivo_rclone.read_bytes()
    conteudos["dependencias_docker/rclone/SHA256SUMS"] = checksums.read_bytes()
    manifesto = {"gerado_em_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "modo": "simulacao", "habilitada": False,
                 "arquivos_sha256": {nome: hashlib.sha256(c).hexdigest() for nome, c in sorted(conteudos.items())}}
    conteudos["manifesto.json"] = (json.dumps(manifesto, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pacote-debian-", dir=destino.parent) as temporaria:
        temporario = Path(temporaria) / destino.name
        with zipfile.ZipFile(temporario, "w", compression=zipfile.ZIP_DEFLATED) as pacote:
            for nome, conteudo in sorted(conteudos.items()):
                pacote.writestr(nome, conteudo)
        with zipfile.ZipFile(temporario) as pacote:
            if pacote.testzip() is not None:
                raise RuntimeError("Falha de integridade no pacote Debian.")
        temporario.replace(destino)
    return destino


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destino", type=Path)
    parser.add_argument("--historico", type=Path)
    args = parser.parse_args()
    try:
        print(gerar_pacote(destino=args.destino, historico=args.historico))
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as erro:
        raise SystemExit(f"Falha ao empacotar Debian: {erro}")


if __name__ == "__main__":
    main()
