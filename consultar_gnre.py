"""Consulta a configuração de uma UF no webservice oficial da GNRE."""

import argparse
from contextlib import closing
import http.client
from pathlib import Path
import re
import ssl
import sys
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


def carregar_xml(conteudo):
    if b"<!DOCTYPE" in conteudo.upper() or b"<!ENTITY" in conteudo.upper():
        raise RuntimeError("O serviço retornou XML com declarações externas.")
    try:
        return ET.fromstring(conteudo)
    except ET.ParseError as erro:
        raise RuntimeError("O serviço retornou um documento XML inválido.") from erro


def requisitar(contexto, host, metodo, caminho, corpo=None, cabecalhos=None):
    with closing(http.client.HTTPSConnection(host, context=contexto, timeout=30)) as conexao:
        conexao.request(metodo, caminho, body=corpo, headers=cabecalhos or {})
        with conexao.getresponse() as resposta:
            dados = resposta.read(LIMITE_RESPOSTA + 1)
            if len(dados) > LIMITE_RESPOSTA:
                raise RuntimeError("A resposta da GNRE excedeu o tamanho esperado.")
            if resposta.status != 200:
                raise RuntimeError(f"O serviço GNRE respondeu HTTP {resposta.status} em {metodo} {caminho}.")
            return dados


def contrato_consulta(conteudo):
    documento = carregar_xml(conteudo)
    if documento.tag != f"{{{WSDL}}}definitions":
        raise RuntimeError("A GNRE não retornou o WSDL esperado para a consulta.")
    namespace = documento.get("targetNamespace")
    if not namespace:
        raise RuntimeError("O WSDL da GNRE não informa o namespace do serviço.")
    for binding in documento.findall(f"{{{WSDL}}}binding"):
        if binding.find(f"{{{WSDL_SOAP}}}binding") is None:
            continue
        operacao = binding.find(f"{{{WSDL}}}operation[@name='consultar']/{{{WSDL_SOAP}}}operation")
        if operacao is not None and operacao.get("soapAction"):
            return namespace, operacao.get("soapAction")
    raise RuntimeError("O WSDL não disponibiliza a operação consultar em SOAP 1.2.")


def montar_consulta(namespace, ambiente, uf, receita):
    envelope = ET.Element(f"{{{SOAP}}}Envelope")
    cabecalho = ET.SubElement(envelope, f"{{{SOAP}}}Header")
    mensagem = ET.SubElement(cabecalho, f"{{{namespace}}}gnreCabecMsg")
    ET.SubElement(mensagem, f"{{{namespace}}}versaoDados").text = "1.00"
    corpo = ET.SubElement(envelope, f"{{{SOAP}}}Body")
    dados = ET.SubElement(corpo, f"{{{namespace}}}gnreDadosMsg")
    consulta = ET.SubElement(dados, f"{{{GNRE}}}TConsultaConfigUf")
    ET.SubElement(consulta, f"{{{GNRE}}}ambiente").text = ambiente
    ET.SubElement(consulta, f"{{{GNRE}}}uf").text = uf
    ET.SubElement(consulta, f"{{{GNRE}}}receita").text = receita
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def analisar_resposta(conteudo, namespace, ambiente, uf):
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
    return descricao


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identidade", type=Path, default=Path("/run/gnre/identidade.pem"))
    parser.add_argument("--ambiente", choices=AMBIENTES, default="producao")
    parser.add_argument("--uf", choices=UFS, default="MS")
    parser.add_argument("--receita", default="100102")
    parser.add_argument("--somente-certificado", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{6}", args.receita):
        parser.error("a receita deve ter seis dígitos")

    try:
        contexto = ssl.create_default_context()
        contexto.minimum_version = ssl.TLSVersion.TLSv1_2
        contexto.load_cert_chain(args.identidade, password=b"")
        print("Certificado e chave privada carregados no serviço.", flush=True)
        if args.somente_certificado:
            return 0
        ambiente, host = AMBIENTES[args.ambiente]
        print(f"Consultando GnreConfigUF em {args.ambiente}, UF {args.uf}, receita {args.receita}.", flush=True)
        namespace, acao = contrato_consulta(requisitar(contexto, host, "GET", SERVICO + "?wsdl"))
        consulta = montar_consulta(namespace, ambiente, args.uf, args.receita)
        resposta = requisitar(
            contexto, host, "POST", SERVICO, consulta,
            {"Content-Type": f'application/soap+xml; charset=utf-8; action="{acao}"'},
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
