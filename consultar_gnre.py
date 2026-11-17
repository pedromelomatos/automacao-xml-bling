"""Consulta a configuração de uma UF no webservice oficial da GNRE."""

import argparse
from contextlib import closing
from email.utils import parsedate_to_datetime
import http.client
import json
from pathlib import Path
import re
import ssl
import sys
import time
import xml.etree.ElementTree as ET


SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSDL = "http://schemas.xmlsoap.org/wsdl/"
WSDL_SOAP = "http://schemas.xmlsoap.org/wsdl/soap12/"
GNRE = "http://www.gnre.pe.gov.br"
SERVICO = "/gnreWS/services/GnreConfigUF"
AMBIENTES = {
    "producao": ("1", "www.gnre.pe.gov.br"),
    "homologacao": ("2", "www.testegnre.pe.gov.br"),
}
UFS = "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
LIMITE_RESPOSTA = 2 * 1024 * 1024


class ErroHttpGnre(RuntimeError):
    def __init__(self, mensagem, status, repetir_apos=0):
        super().__init__(mensagem)
        self.status, self.repetir_apos = status, repetir_apos


def espera_http(valor):
    try:
        return max(0, int(valor))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(valor).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


def carregar_xml(conteudo):
    if b"<!DOCTYPE" in conteudo.upper() or b"<!ENTITY" in conteudo.upper():
        raise RuntimeError("O serviço retornou XML com declarações externas.")
    try:
        return ET.fromstring(conteudo)
    except ET.ParseError as erro:
        raise RuntimeError("O serviço retornou um documento XML inválido.") from erro


def requisitar(contexto, host, metodo, caminho, corpo=None, cabecalhos=None, timeout=90, diagnostico=False,
               tentativas=2, etapa=None, limite_resposta=LIMITE_RESPOSTA):
    etapa = etapa or ("leitura do WSDL" if metodo == "GET" else "consulta das configurações da UF")
    for tentativa in range(1, tentativas + 1):
        print(f"Etapa de {etapa}, tentativa {tentativa}/{tentativas}, limite de espera de {timeout}s.", flush=True)
        inicio = time.monotonic()
        try:
            with closing(http.client.HTTPSConnection(host, context=contexto, timeout=timeout)) as conexao:
                conexao.request(metodo, caminho, body=corpo, headers=cabecalhos or {})
                with conexao.getresponse() as resposta:
                    dados = resposta.read(limite_resposta + 1)
                    if len(dados) > limite_resposta:
                        raise RuntimeError("A resposta da GNRE excedeu o tamanho esperado.")
                    if diagnostico:
                        detalhes = {
                            "metodo": metodo,
                            "url": f"https://{host}{caminho}",
                            "status": resposta.status,
                            "segundos": round(time.monotonic() - inicio, 2),
                            "cabecalhos": {
                                nome: resposta.getheader(nome)
                                for nome in ("Server", "Content-Type", "Content-Length", "Date", "Via", "Retry-After")
                            },
                        }
                        if resposta.status != 200:
                            detalhes["corpo"] = dados[:2048].decode("utf-8", errors="replace")
                        print("Diagnóstico HTTP " + json.dumps(detalhes, ensure_ascii=False), flush=True)
                    if resposta.status in (502, 503, 504) and tentativa < tentativas:
                        print(f"HTTP {resposta.status} na etapa de {etapa}. Repetindo em 2 segundos.", flush=True)
                        time.sleep(2)
                        continue
                    if resposta.status != 200:
                        raise ErroHttpGnre(
                            f"O serviço GNRE respondeu HTTP {resposta.status} na etapa de {etapa} "
                            f"em {metodo} {caminho}, tentativa {tentativa}/{tentativas}.",
                            resposta.status, espera_http(resposta.getheader('Retry-After')),
                        )
                    return dados
        except TimeoutError as erro:
            if tentativa == tentativas:
                raise RuntimeError(
                    f"Tempo esgotado na etapa de {etapa}, após {tentativas} tentativa(s) com espera de {timeout}s."
                ) from erro
            print("Tempo de espera esgotado. Repetindo a consulta.", flush=True)


def contrato_consulta(conteudo, operacao="consultar"):
    documento = carregar_xml(conteudo)
    if documento.tag != f"{{{WSDL}}}definitions":
        raise RuntimeError("A GNRE não retornou o WSDL esperado para a consulta.")
    namespace = documento.get("targetNamespace")
    if not namespace:
        raise RuntimeError("O WSDL da GNRE não informa o namespace do serviço.")
    for binding in documento.findall(f"{{{WSDL}}}binding"):
        if binding.find(f"{{{WSDL_SOAP}}}binding") is None:
            continue
        chamada = binding.find(f"{{{WSDL}}}operation[@name='{operacao}']/{{{WSDL_SOAP}}}operation")
        if chamada is not None and chamada.get("soapAction"):
            return namespace, chamada.get("soapAction")
    raise RuntimeError(f"O WSDL não disponibiliza a operação {operacao} em SOAP 1.2.")


def diagnosticar_contrato(conteudo, namespace, acao):
    documento = carregar_xml(conteudo)
    portas = []
    for porta in documento.findall(f"{{{WSDL}}}service/{{{WSDL}}}port"):
        for endereco in porta:
            if endereco.tag.endswith("}address"):
                portas.append({
                    "porta": porta.get("name"),
                    "binding": porta.get("binding"),
                    "url": endereco.get("location"),
                    "protocolo": endereco.tag,
                })
    mensagens = [
        {"nome": mensagem.get("name"), "partes": [parte.attrib for parte in mensagem]}
        for mensagem in documento.findall(f"{{{WSDL}}}message")
    ]
    dados = {"namespace": namespace, "acao": acao, "portas": portas, "mensagens": mensagens}
    print("Diagnóstico WSDL " + json.dumps(dados, ensure_ascii=False), flush=True)


def montar_consulta(namespace, ambiente, uf, receita):
    envelope = ET.Element("soap12:Envelope", {"xmlns:soap12": SOAP})
    cabecalho = ET.SubElement(envelope, "soap12:Header")
    mensagem = ET.SubElement(cabecalho, "gnreCabecMsg", {"xmlns": namespace})
    ET.SubElement(mensagem, "versaoDados").text = "1.00"
    corpo = ET.SubElement(envelope, "soap12:Body")
    dados = ET.SubElement(corpo, "gnreDadosMsg", {"xmlns": namespace})
    # O leiaute GNRE exige namespace padrão, sem prefixos nos dados da consulta.
    consulta = ET.SubElement(dados, "TConsultaConfigUf", {"xmlns": GNRE})
    ET.SubElement(consulta, "ambiente").text = ambiente
    ET.SubElement(consulta, "uf").text = uf
    ET.SubElement(consulta, "receita").text = receita
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def obter_configuracao(conteudo, namespace, ambiente, uf):
    documento = carregar_xml(conteudo)
    if documento.tag != f"{{{SOAP}}}Envelope":
        raise RuntimeError("O serviço não retornou um envelope SOAP 1.2.")
    corpo = documento.find(f"{{{SOAP}}}Body")
    if corpo is None:
        raise RuntimeError("A resposta da GNRE está sem o corpo SOAP.")
    falha = corpo.find(f"{{{SOAP}}}Fault")
    if falha is not None:
        motivo = falha.findtext(f"{{{SOAP}}}Reason/{{{SOAP}}}Text", default="motivo não informado")
        raise RuntimeError(f"Falha SOAP da GNRE: {motivo}")
    mensagem = corpo.find(f".//{{{namespace}}}gnreRespostaMsg")
    if mensagem is None:
        raise RuntimeError("A resposta não contém gnreRespostaMsg.")
    configuracao = mensagem.find(f"{{{GNRE}}}TConfigUf")
    if configuracao is None and mensagem.text and mensagem.text.strip().startswith("<"):
        configuracao = carregar_xml(mensagem.text.encode("utf-8"))
    if configuracao is None or configuracao.tag != f"{{{GNRE}}}TConfigUf":
        raise RuntimeError("A resposta não contém a configuração da UF.")
    situacao = configuracao.find(f"{{{GNRE}}}situacaoConsulta")
    if situacao is None:
        raise RuntimeError("A resposta não informa a situação da consulta.")
    codigo = situacao.findtext(f"{{{GNRE}}}codigo") or situacao.findtext(f"{{{GNRE}}}Codigo")
    descricao = situacao.findtext(f"{{{GNRE}}}descricao", default="descrição não informada")
    if codigo != "450":
        raise RuntimeError(f"Consulta GNRE rejeitada ({codigo}): {descricao}")
    ambiente_retornado = configuracao.findtext(f"{{{GNRE}}}ambiente") or configuracao.findtext(f"{{{GNRE}}}Ambiente")
    uf_retornada = configuracao.findtext(f"{{{GNRE}}}uf") or configuracao.findtext(f"{{{GNRE}}}Uf")
    if ambiente_retornado != ambiente or uf_retornada != uf:
        raise RuntimeError("A resposta da GNRE corresponde a outro ambiente ou outra UF.")
    return configuracao


def analisar_resposta(conteudo, namespace, ambiente, uf):
    configuracao = obter_configuracao(conteudo, namespace, ambiente, uf)
    return configuracao.findtext(f"{{{GNRE}}}situacaoConsulta/{{{GNRE}}}descricao", default="descrição não informada")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identidade", type=Path, default=Path("/run/gnre/identidade.pem"))
    parser.add_argument("--ambiente", choices=AMBIENTES, default="producao")
    parser.add_argument("--uf", choices=UFS, default="MS")
    parser.add_argument("--receita", default="100102")
    parser.add_argument("--somente-certificado", action="store_true")
    parser.add_argument("--timeout", type=int, default=90, help="espera de cada operação HTTPS, em segundos (1 a 300)")
    parser.add_argument("--diagnostico", action="store_true", help="mostra o contrato WSDL, o pedido SOAP e detalhes HTTP")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{6}", args.receita):
        parser.error("a receita deve ter seis dígitos")
    if not 1 <= args.timeout <= 300:
        parser.error("o timeout deve estar entre 1 e 300 segundos")

    try:
        contexto = ssl.create_default_context()
        contexto.minimum_version = ssl.TLSVersion.TLSv1_2
        contexto.load_cert_chain(args.identidade, password=b"")
        print("Certificado e chave privada carregados no serviço.", flush=True)
        if args.somente_certificado:
            return 0
        ambiente, host = AMBIENTES[args.ambiente]
        print(f"Consultando GnreConfigUF em {args.ambiente}, UF {args.uf}, receita {args.receita}.", flush=True)
        wsdl = requisitar(
            contexto, host, "GET", SERVICO + "?wsdl", timeout=args.timeout, diagnostico=args.diagnostico
        )
        namespace, acao = contrato_consulta(wsdl)
        if args.diagnostico:
            diagnosticar_contrato(wsdl, namespace, acao)
        consulta = montar_consulta(namespace, ambiente, args.uf, args.receita)
        if args.diagnostico:
            print("Pedido SOAP " + json.dumps(consulta.decode("utf-8"), ensure_ascii=False), flush=True)
        resposta = requisitar(
            contexto, host, "POST", SERVICO, consulta,
            {"Content-Type": f'application/soap+xml; charset=utf-8; action="{acao}"'},
            timeout=args.timeout, diagnostico=args.diagnostico,
        )
        descricao = analisar_resposta(resposta, namespace, ambiente, args.uf)
        print(f"Consulta aceita pela GNRE (450). {descricao}")
    except ssl.SSLError as erro:
        print(f"Falha TLS ao carregar o certificado ou conectar à GNRE: {erro}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, http.client.HTTPException) as erro:
        print(f"Falha na consulta GNRE: {erro}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
