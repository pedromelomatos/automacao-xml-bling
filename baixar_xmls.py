"""Baixa os XMLs das NF-e emitidas em um dia no Bling."""

import argparse
import base64
import binascii
import gzip
import logging
import os
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as horario, timedelta
from pathlib import Path
from pathlib import PurePosixPath
from xml.etree import ElementTree

import requests

from bling_auth import obter_access_token
from organizar_xmls import (
    carregar_configuracao,
    limpar_componente_pasta,
    organizar_xml,
)


BASE_DIR = Path(__file__).resolve().parent


def obter_pasta_dados():
    configurada = os.getenv("AUTOMACAO_DATA_DIR")
    if configurada:
        return Path(configurada).expanduser().resolve()
    compartilhada = BASE_DIR.parent / "dados"
    if compartilhada.is_dir():
        return compartilhada.resolve()
    return BASE_DIR


DATA_DIR = obter_pasta_dados()
BASE_URL = "https://api.bling.com.br/Api/v3"
INTERVALO_REQUISICOES = 0.45
MAX_TENTATIVAS = 3


def obter_argumentos():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data",
        nargs="?",
        type=date.fromisoformat,
        default=date.today(),
        help="data de emissão no formato AAAA-MM-DD (padrão: hoje)",
    )
    parser.add_argument(
        "--janela-14h",
        action="store_true",
        help="consulta das 14h do dia anterior às 14h da data informada",
    )
    parser.add_argument(
        "--ate-agora",
        action="store_true",
        help="modo de teste: encerra a janela no horário atual se ainda não forem 14h",
    )
    parser.add_argument(
        "--sem-gnre",
        action="store_true",
        help="organiza as NF-e sem criar as cópias adicionais na pasta GNRE",
    )
    parser.add_argument(
        "--somente-gnre-zip",
        action="store_true",
        help="gera somente as GNRE em um arquivo ZIP por unidade",
    )
    return parser.parse_args()


def calcular_periodo(data_consulta, janela_14h, ate_agora=False, agora=None):
    agora = agora or datetime.now()
    if ate_agora and not janela_14h:
        raise RuntimeError("Use --ate-agora junto com --janela-14h.")

    if janela_14h:
        inicio = datetime.combine(data_consulta - timedelta(days=1), horario(14))
        corte = datetime.combine(data_consulta, horario(14))
        if ate_agora:
            fim = min(agora.replace(microsecond=0), corte)
            if fim <= inicio:
                raise RuntimeError("A janela de teste ainda não começou.")
        else:
            fim = corte
            if fim > agora:
                raise RuntimeError(
                    "A janela das 14h ainda não terminou. "
                    "Para um teste parcial, acrescente --ate-agora."
                )
        return inicio, fim

    return (
        datetime.combine(data_consulta, horario.min),
        datetime.combine(data_consulta, horario(23, 59, 59)),
    )


def configurar_log(data_consulta, janela_14h=False, handler_adicional=None):
    pasta_logs = DATA_DIR / "logs"
    pasta_logs.mkdir(parents=True, exist_ok=True)
    sufixo = "_14h" if janela_14h else ""
    arquivo_log = pasta_logs / f"automacao_{data_consulta.isoformat()}{sufixo}.log"
    handlers = [
        logging.FileHandler(arquivo_log, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    if handler_adicional is not None:
        handlers.append(handler_adicional)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    return arquivo_log


@dataclass(frozen=True)
class ResultadoDownload:
    data_consulta: date
    pasta_destino: Path
    pasta_organizada: Path
    arquivo_log: Path
    total_notas: int
    baixados: int
    existentes: int
    sem_chave: int
    erros: int
    organizados: int
    organizacao_existente: int
    erros_organizacao: int
    arquivos_zip_gnre: tuple[Path, ...]
    gnres_no_zip: int
    somente_gnre_zip: bool

    @property
    def codigo_saida(self):
        return 1 if self.erros or self.sem_chave or self.erros_organizacao else 0

    @property
    def pasta_resultado(self):
        if self.arquivos_zip_gnre:
            return self.arquivos_zip_gnre[0].parent
        return self.pasta_organizada


def criar_zips_gnre(registros, data_consulta, pasta_saida=None):
    """Cria um ZIP por unidade com os XMLs de GNRE selecionados."""
    pasta_saida = Path(pasta_saida or (DATA_DIR / "GNRE - ZIP"))
    por_unidade = defaultdict(list)
    for unidade, marketplace, uf_destino, caminho_xml in registros:
        por_unidade[unidade].append(
            (marketplace, uf_destino, Path(caminho_xml))
        )

    if not por_unidade:
        return ()

    pasta_saida.mkdir(parents=True, exist_ok=True)
    arquivos_zip = []
    sufixo_data = data_consulta.strftime("%d-%m-%Y")

    for unidade, arquivos in sorted(por_unidade.items()):
        nome_unidade = limpar_componente_pasta(unidade)
        caminho_zip = pasta_saida / f"{nome_unidade} {sufixo_data}.zip"
        temporario = caminho_zip.with_suffix(".zip.tmp")
        nomes_incluidos = set()
        try:
            with zipfile.ZipFile(
                temporario,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as arquivo_zip:
                for marketplace, uf_destino, caminho_xml in sorted(
                    arquivos,
                    key=lambda item: (
                        str(item[0]),
                        str(item[1]),
                        item[2].name,
                    ),
                ):
                    nome_interno = str(
                        PurePosixPath(
                            limpar_componente_pasta(marketplace),
                            limpar_componente_pasta(uf_destino),
                            caminho_xml.name,
                        )
                    )
                    if nome_interno in nomes_incluidos:
                        continue
                    arquivo_zip.write(caminho_xml, arcname=nome_interno)
                    nomes_incluidos.add(nome_interno)
            temporario.replace(caminho_zip)
        finally:
            temporario.unlink(missing_ok=True)
        arquivos_zip.append(caminho_zip)

    return tuple(arquivos_zip)


class ClienteBling:
    def __init__(self, access_token):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "*/*",
                "enable-jwt": "1",
            }
        )
        self.ultima_requisicao = 0.0

    def get(self, caminho, **kwargs):
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            espera = INTERVALO_REQUISICOES - (time.monotonic() - self.ultima_requisicao)
            if espera > 0:
                time.sleep(espera)

            self.ultima_requisicao = time.monotonic()
            try:
                resposta = self.session.get(
                    f"{BASE_URL}{caminho}", timeout=30, **kwargs
                )
            except requests.RequestException as erro:
                if tentativa == MAX_TENTATIVAS:
                    raise RuntimeError("Falha de conexão após 3 tentativas.") from erro
                logging.warning("Falha de conexão; tentando novamente.")
                time.sleep(2 * tentativa)
                continue

            if resposta.status_code == 429 or 500 <= resposta.status_code <= 599:
                if tentativa == MAX_TENTATIVAS:
                    raise RuntimeError(f"HTTP {resposta.status_code} após 3 tentativas.")
                espera = 2 * tentativa
                if resposta.status_code == 429:
                    retry_after = resposta.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        espera = max(espera, min(int(retry_after), 60))
                logging.warning("HTTP %s; nova tentativa em %ss.", resposta.status_code, espera)
                time.sleep(espera)
                continue

            if not resposta.ok:
                raise RuntimeError(f"HTTP {resposta.status_code}.")
            return resposta

        raise RuntimeError("Falha inesperada na requisição.")

    def close(self):
        self.session.close()


def consultar_nfes(cliente, inicio, fim):
    notas = []
    pagina = 1

    while True:
        logging.info("Consultando página %s das NF-e.", pagina)
        resposta = cliente.get(
            "/nfe",
            params={
                "pagina": pagina,
                "limite": 100,
                "tipo": 1,
                "dataEmissaoInicial": inicio.strftime("%Y-%m-%d %H:%M:%S"),
                "dataEmissaoFinal": fim.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        pagina_notas = resposta.json().get("data")
        if not isinstance(pagina_notas, list):
            raise RuntimeError("Resposta da consulta sem lista de NF-e.")
        notas.extend(pagina_notas)
        if len(pagina_notas) < 100:
            break
        pagina += 1

    return notas


def filtrar_emitidas_na_janela(notas, inicio, fim):
    selecionadas = []
    fora_do_escopo = 0
    datas_invalidas = 0
    for nota in notas:
        try:
            emissao = datetime.fromisoformat(nota["dataEmissao"])
            if emissao.tzinfo is not None:
                raise ValueError("data com fuso horário inesperado")
        except (KeyError, TypeError, ValueError):
            datas_invalidas += 1
            continue

        # O Bling pode incluir o instante final; a janela local termina antes dele.
        if not inicio <= emissao < fim or str(nota.get("situacao")) not in {"5", "6"}:
            fora_do_escopo += 1
            continue
        selecionadas.append(nota)
    return selecionadas, fora_do_escopo, datas_invalidas


def xml_valido(conteudo):
    try:
        raiz = ElementTree.fromstring(conteudo)
    except ElementTree.ParseError:
        return False
    nome_raiz = raiz.tag.rsplit("}", 1)[-1].lower()
    return nome_raiz in {"nfeproc", "nfe"}


def arquivo_xml_valido(caminho):
    try:
        return xml_valido(caminho.read_bytes())
    except OSError:
        return False


def baixar_xml(cliente, chave, caminho):
    resposta = cliente.get(f"/nfe/documento/{chave}", params={"formato": "xml"})
    conteudo = extrair_xml(resposta)
    temporario = caminho.with_suffix(".xml.tmp")
    temporario.write_bytes(conteudo)
    temporario.replace(caminho)


def extrair_xml(resposta):
    if xml_valido(resposta.content):
        return resposta.content

    try:
        dados = resposta.json()["data"]
        documento = dados[0] if isinstance(dados, list) else dados
        codificado = documento["conteudo"]
        conteudo = base64.b64decode(codificado, validate=True)
        if conteudo.startswith(b"\x1f\x8b"):
            conteudo = gzip.decompress(conteudo)
    except (ValueError, KeyError, IndexError, TypeError, binascii.Error, OSError) as erro:
        raise RuntimeError("Formato inesperado da resposta de download.") from erro

    if not xml_valido(conteudo):
        raise RuntimeError("Resposta de download não contém uma NF-e XML válida.")
    return conteudo


def processar_download(
    data_consulta,
    janela_14h=False,
    ate_agora=False,
    incluir_gnre=True,
    somente_gnre_zip=False,
    handler_log=None,
):
    inicio, fim = calcular_periodo(
        data_consulta,
        janela_14h,
        ate_agora,
    )
    os.chdir(BASE_DIR)  # bling_auth.py localiza tokens.json pela pasta atual.
    arquivo_log = configurar_log(data_consulta, janela_14h, handler_log)
    pasta_destino = (
        DATA_DIR
        / ("xml_originais" if janela_14h else "xml")
        / data_consulta.strftime("%d-%m-%Y")
    )
    pasta_destino.mkdir(parents=True, exist_ok=True)
    logging.info("Iniciando download dos XMLs de %s até %s.", inicio, fim)

    cliente = ClienteBling(obter_access_token())
    try:
        notas = consultar_nfes(cliente, inicio, fim)
        logging.info("NF-e retornadas pela consulta: %s", len(notas))
        if janela_14h:
            notas, fora_do_escopo, datas_invalidas = filtrar_emitidas_na_janela(
                notas, inicio, fim
            )
            logging.info(
                "NF-e autorizadas ou Emitida DANFE na janela: %s | fora do escopo: %s | datas inválidas: %s",
                len(notas), fora_do_escopo, datas_invalidas,
            )
        else:
            datas_invalidas = 0
        baixados = existentes = sem_chave = 0
        erros = datas_invalidas
        organizados = organizacao_existente = erros_organizacao = 0
        chaves_vistas = set()
        registros_gnre = []
        configuracao = carregar_configuracao()

        for indice, nota in enumerate(notas, start=1):
            numero = nota.get("numero", "?")
            chave = str(nota.get("chaveAcesso") or "")
            if len(chave) != 44 or not chave.isdecimal():
                sem_chave += 1
                logging.warning("[%s/%s] NF-e %s sem chave válida.", indice, len(notas), numero)
                continue
            if chave in chaves_vistas:
                continue
            chaves_vistas.add(chave)

            caminho = pasta_destino / f"{chave}.xml"
            if caminho.exists() and arquivo_xml_valido(caminho):
                existentes += 1
                logging.info("[%s/%s] NF-e %s: XML já existe.", indice, len(notas), numero)
            else:
                try:
                    baixar_xml(cliente, chave, caminho)
                    baixados += 1
                    logging.info("[%s/%s] NF-e %s: XML salvo.", indice, len(notas), numero)
                except (OSError, RuntimeError) as erro:
                    erros += 1
                    logging.error("[%s/%s] NF-e %s: falha no download: %s", indice, len(notas), numero, erro)
                    continue

            if janela_14h:
                try:
                    resultado = organizar_xml(
                        caminho,
                        nota=nota,
                        configuracao=configuracao,
                        incluir_gnre=incluir_gnre or somente_gnre_zip,
                        somente_gnre=somente_gnre_zip,
                    )
                    organizados += resultado.novos
                    organizacao_existente += resultado.existentes
                    if somente_gnre_zip:
                        for destino in resultado.destinos:
                            registros_gnre.append(
                                (
                                    resultado.unidade,
                                    resultado.marketplace,
                                    resultado.uf_destino,
                                    destino,
                                )
                            )
                    if resultado.aviso:
                        logging.warning("NF-e %s: %s.", numero, resultado.aviso)
                except (OSError, RuntimeError) as erro:
                    erros_organizacao += 1
                    logging.error("NF-e %s: falha na organização: %s", numero, erro)

        arquivos_zip_gnre = (
            criar_zips_gnre(registros_gnre, data_consulta)
            if somente_gnre_zip
            else ()
        )

        logging.info(
            "Finalizado | NF-e: %s | baixados: %s | existentes: %s | sem chave válida: %s | erros: %s",
            len(notas), baixados, existentes, sem_chave, erros,
        )
        if janela_14h:
            logging.info(
                "Organização | cópias criadas: %s | cópias existentes: %s | erros: %s",
                organizados, organizacao_existente, erros_organizacao,
            )
        if somente_gnre_zip:
            if arquivos_zip_gnre:
                logging.info(
                    "GNRE | XMLs incluídos: %s | ZIPs: %s",
                    len(registros_gnre),
                    ", ".join(str(caminho) for caminho in arquivos_zip_gnre),
                )
            else:
                logging.info("GNRE | Nenhum XML elegível para a data selecionada.")
        logging.info("XMLs: %s | Log: %s", pasta_destino, arquivo_log)
        return ResultadoDownload(
            data_consulta=data_consulta,
            pasta_destino=pasta_destino,
            pasta_organizada=DATA_DIR / "xml_por_cnpj",
            arquivo_log=arquivo_log,
            total_notas=len(notas),
            baixados=baixados,
            existentes=existentes,
            sem_chave=sem_chave,
            erros=erros,
            organizados=organizados,
            organizacao_existente=organizacao_existente,
            erros_organizacao=erros_organizacao,
            arquivos_zip_gnre=arquivos_zip_gnre,
            gnres_no_zip=len(registros_gnre),
            somente_gnre_zip=somente_gnre_zip,
        )
    finally:
        cliente.close()


def main():
    argumentos = obter_argumentos()
    janela_14h = argumentos.janela_14h or argumentos.somente_gnre_zip
    resultado = processar_download(
        argumentos.data,
        janela_14h=janela_14h,
        ate_agora=argumentos.ate_agora,
        incluir_gnre=not argumentos.sem_gnre,
        somente_gnre_zip=argumentos.somente_gnre_zip,
    )
    return resultado.codigo_saida


if __name__ == "__main__":
    sys.exit(main())




# Para executar a janela operacional diária específica e também organizar os XMLs por CNPJ, marketplace, data, UF e GNRE, use:
# python baixar_xmls.py 2026-09-10 --janela-14h
