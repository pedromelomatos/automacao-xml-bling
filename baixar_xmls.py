"""Baixa os XMLs das NF-e emitidas em um dia no Bling."""

import argparse
import base64
import binascii
import gzip
import logging
import os
import sys
import time
from datetime import date, datetime, time as horario, timedelta
from pathlib import Path
from xml.etree import ElementTree

import requests

from bling_auth import obter_access_token
from organizar_xmls import carregar_configuracao, organizar_xml


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


def configurar_log(data_consulta, janela_14h=False):
    pasta_logs = DATA_DIR / "logs"
    pasta_logs.mkdir(parents=True, exist_ok=True)
    sufixo = "_14h" if janela_14h else ""
    arquivo_log = pasta_logs / f"automacao_{data_consulta.isoformat()}{sufixo}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(arquivo_log, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return arquivo_log


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


def main():
    argumentos = obter_argumentos()
    data_consulta = argumentos.data
    inicio, fim = calcular_periodo(
        data_consulta,
        argumentos.janela_14h,
        argumentos.ate_agora,
    )
    os.chdir(BASE_DIR)  # bling_auth.py localiza tokens.json pela pasta atual.
    arquivo_log = configurar_log(data_consulta, argumentos.janela_14h)
    pasta_destino = (
        DATA_DIR
        / ("xml_originais" if argumentos.janela_14h else "xml")
        / data_consulta.strftime("%d-%m-%Y")
    )
    pasta_destino.mkdir(parents=True, exist_ok=True)
    logging.info("Iniciando download dos XMLs de %s até %s.", inicio, fim)

    cliente = ClienteBling(obter_access_token())
    try:
        notas = consultar_nfes(cliente, inicio, fim)
        logging.info("NF-e retornadas pela consulta: %s", len(notas))
        if argumentos.janela_14h:
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

            if argumentos.janela_14h:
                try:
                    resultado = organizar_xml(
                        caminho,
                        nota=nota,
                        configuracao=configuracao,
                    )
                    organizados += resultado.novos
                    organizacao_existente += resultado.existentes
                    if resultado.aviso:
                        logging.warning("NF-e %s: %s.", numero, resultado.aviso)
                except (OSError, RuntimeError) as erro:
                    erros_organizacao += 1
                    logging.error("NF-e %s: falha na organização: %s", numero, erro)

        logging.info(
            "Finalizado | NF-e: %s | baixados: %s | existentes: %s | sem chave válida: %s | erros: %s",
            len(notas), baixados, existentes, sem_chave, erros,
        )
        if argumentos.janela_14h:
            logging.info(
                "Organização | cópias criadas: %s | cópias existentes: %s | erros: %s",
                organizados, organizacao_existente, erros_organizacao,
            )
        logging.info("XMLs: %s | Log: %s", pasta_destino, arquivo_log)
        return 1 if erros or sem_chave or erros_organizacao else 0
    finally:
        cliente.close()


if __name__ == "__main__":
    sys.exit(main())




# Para executar a janela operacional diária específica e também organizar os XMLs por CNPJ, marketplace, data, UF e GNRE, use:
# python baixar_xmls.py 2026-09-10 --janela-14h
