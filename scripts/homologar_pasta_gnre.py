"""Atalho de homologação chamado pelo PowerShell; preserva os controles existentes."""

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo


ENSAIO = "notas-13-11-2026-em-2026-11-16"


def registros_da_nota(raiz, chave):
    registros = []
    for banco in sorted((raiz / "dados-gnre").rglob("controle.sqlite3")):
        # Nunca criar, migrar ou modificar bases históricas durante a busca.
        with closing(sqlite3.connect(banco.resolve().as_uri() + "?mode=ro", uri=True)) as conexao:
            conexao.row_factory = sqlite3.Row
            for linha in conexao.execute(
                "SELECT id, estado, recibo, pasta, origens FROM lotes WHERE ambiente='homologacao'"
            ):
                origens = json.loads(linha["origens"])
                if any(identidade.split("|")[-1] == chave for identidade in origens.values()):
                    registros.append({**dict(linha), "dados": str(banco.parent)})
    return registros


def executar_cliente(cliente, dados, identidade, argumentos):
    return subprocess.run([
        sys.executable, "-u", str(cliente),
        "--identidade", str(identidade), "--dados", str(dados),
        "--ambiente", "homologacao", *argumentos,
    ], check=False).returncode


def processar_xml(xml, raiz, cliente, dados, identidade, data_teste, simular, ler_nfe):
    nota = ler_nfe(xml)
    registros = registros_da_nota(raiz, nota["chave"])
    resultado = {"arquivo": xml.name, "chave": nota["chave"], "uf": nota["uf"]}
    if simular:
        resultado.update(acao="ja_registrada" if registros else "novo_envio", codigo=0, lotes=registros)
        print(f"Simulação: {xml.name}, {nota['uf']}: {resultado['acao']}.", flush=True)
        return resultado

    if registros:
        print(f"{xml.name}: nota já registrada; preservando os envios existentes.", flush=True)
        codigos = []
        for registro in registros:
            if registro["estado"] == "concluido":
                codigos.append(0)
            elif registro["estado"] in ("com_rejeicoes", "rejeitado", "em_revisao"):
                print(f"Lote {registro['id']}: {registro['estado']}; requer revisão.", flush=True)
                codigos.append(1)
            elif registro["recibo"]:
                codigos.append(executar_cliente(cliente, registro["dados"], identidade, [
                    "consultar", "--lote-id", registro["id"], "--aguardar", "120",
                ]))
            else:
                print(f"Lote {registro['id']}: sem recibo; conferir o registro antes de qualquer envio.", flush=True)
                codigos.append(1)
        codigo = 1 if any(c not in (0, 2) for c in codigos) else max(codigos)
        resultado["acao"] = "registro_existente"
    else:
        codigo = executar_cliente(cliente, dados, identidade, [
            "executar", "--xml", str(xml), "--vencimento", data_teste,
            "--pagamento", data_teste, "--aguardar", "120",
        ])
        resultado["acao"] = "novo_envio"
    resultado.update(codigo=codigo, lotes=registros_da_nota(raiz, nota["chave"]))
    return resultado


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pasta", type=Path)
    parser.add_argument("--simular", action="store_true", help="Confere XMLs e registros, sem acessar a GNRE")
    args = parser.parse_args()
    raiz = Path.home() / "gnre-emissao"
    dados = raiz / "dados-gnre" / "ensaios" / ENSAIO
    cliente = dados / "cliente" / "emitir_gnre.py"
    identidade = Path.home() / ".local/share/automacao-xml-bling/gnre/identidade.pem"
    saida = args.pasta.resolve() / "resultados"
    saida.mkdir(mode=0o700, parents=True, exist_ok=True)
    resultados = []
    try:
        if not cliente.is_file() or not (dados / "controle.sqlite3").is_file():
            raise RuntimeError("Cliente ou controle persistente ausente. Confira a instalação; não crie outra base.")
        runtime = str(raiz / ".runtime/python313")
        sys.path[:0] = [str(cliente.parent), runtime]
        os.environ["PYTHONPATH"] = runtime
        from preparar_gnre import ler_nfe
        import fcntl

        # Evita duas execuções simultâneas deste atalho.
        with (raiz / ".homologar-pasta.lock").open("a") as trava:
            fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data_teste = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
            print(f"GNRE em HOMOLOGAÇÃO. Datas do teste: {data_teste}. Guias inválidas para pagamento.", flush=True)
            xmls = sorted(p for p in args.pasta.iterdir() if p.is_file() and p.suffix.lower() == ".xml")
            if not xmls:
                raise RuntimeError("Nenhum XML encontrado.")
            for xml in xmls:
                try:
                    resultado = processar_xml(
                        xml, raiz, cliente, dados, identidade, data_teste, args.simular, ler_nfe,
                    )
                    if not args.simular:
                        for registro in resultado["lotes"]:
                            for pdf in Path(registro["pasta"]).glob("guias-*.pdf"):
                                shutil.copy2(pdf, saida / f"{registro['id']}-{pdf.name}")
                    resultados.append(resultado)
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as erro:
                    print(f"{xml.name}: {erro}", flush=True)
                    resultados.append({"arquivo": xml.name, "codigo": 1, "erro": str(erro)})
    except (OSError, RuntimeError, ValueError, sqlite3.Error, ImportError) as erro:
        print(f"Falha: {erro}", flush=True)
        resultados.append({"codigo": 1, "erro": str(erro)})
    finally:
        (saida / "resumo.json").write_text(
            json.dumps({"ambiente": "homologacao", "simulacao": args.simular, "resultados": resultados},
                       ensure_ascii=False, indent=2), encoding="utf-8",
        )
    codigos = [r["codigo"] for r in resultados]
    return 1 if any(c not in (0, 2) for c in codigos) else max(codigos, default=0)


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
