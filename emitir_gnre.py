"""Prepara, transmite e acompanha lotes GNRE, com recibos persistentes."""

import argparse
import base64
from contextlib import closing
from datetime import date
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from lxml import etree

import consultar_gnre as consulta
from preparar_gnre import DOCUMENTOS_CHAVE_NFE, DOCUMENTOS_NUMERO_NFE, GNRE, RECEITAS, ler_nfe, montar_lote, texto, validar_xsd


SERVICOS = {
    "recepcao": ("GnreLoteRecepcao", "processar", "2.00"),
    "resultado": ("GnreResultadoLote", "consultar", "2.00"),
}
LIMITE_RESULTADO = 24 * 1024 * 1024


def gravar(caminho, conteudo):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(dir=caminho.parent, prefix=".gnre-", delete=False) as arquivo:
            temporario = Path(arquivo.name)
            arquivo.write(conteudo)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        temporario.replace(caminho)
    finally:
        if temporario:
            temporario.unlink(missing_ok=True)


def envelope(namespace, versao, dados):
    raiz = etree.Element(f"{{{consulta.SOAP}}}Envelope", nsmap={"soap12": consulta.SOAP})
    cabecalho = etree.SubElement(raiz, f"{{{consulta.SOAP}}}Header")
    mensagem = etree.SubElement(cabecalho, f"{{{namespace}}}gnreCabecMsg", nsmap={None: namespace})
    etree.SubElement(mensagem, f"{{{namespace}}}versaoDados").text = versao
    corpo = etree.SubElement(raiz, f"{{{consulta.SOAP}}}Body")
    mensagem = etree.SubElement(corpo, f"{{{namespace}}}gnreDadosMsg", nsmap={None: namespace})
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    consulta.carregar_xml(dados)
    mensagem.append(etree.fromstring(dados, parser))
    return etree.tostring(raiz, encoding="utf-8", xml_declaration=True)


def extrair_retorno(conteudo, nome):
    raiz = consulta.carregar_xml(conteudo)
    if raiz.tag != f"{{{consulta.SOAP}}}Envelope":
        raise RuntimeError("Retorno da GNRE sem envelope SOAP 1.2.")
    corpo = raiz.find(f"{{{consulta.SOAP}}}Body")
    if corpo is None:
        raise RuntimeError("Retorno da GNRE sem corpo SOAP.")
    falha = corpo.find(f"{{{consulta.SOAP}}}Fault")
    if falha is not None:
        motivo = falha.findtext(f"{{{consulta.SOAP}}}Reason/{{{consulta.SOAP}}}Text", default="motivo não informado")
        raise RuntimeError(f"Falha SOAP: {motivo}")
    for elemento in corpo.iter():
        if elemento.tag.startswith(f"{{{GNRE}}}") and elemento.tag.rsplit("}", 1)[-1].lower() == nome.lower():
            return elemento
        if elemento.text and elemento.text.strip().startswith("<"):
            interno = consulta.carregar_xml(elemento.text.encode("utf-8"))
            if interno.tag == f"{{{GNRE}}}{nome}":
                return interno
    raise RuntimeError(f"O retorno da GNRE não contém {nome}.")


class ServicoGnre:
    def __init__(self, identidade, ambiente, timeout=90):
        self.ambiente, self.host = consulta.AMBIENTES[ambiente]
        self.timeout = timeout
        self.contexto = ssl.create_default_context()
        self.contexto.minimum_version = ssl.TLSVersion.TLSv1_2
        self.contexto.load_cert_chain(identidade, password=b"")
        self.contratos = {}

    def preparar(self, servico):
        nome, operacao, _ = SERVICOS[servico]
        caminho = "/gnreWS/services/" + nome
        if nome not in self.contratos:
            wsdl = consulta.requisitar(self.contexto, self.host, "GET", caminho + "?wsdl", timeout=self.timeout)
            self.contratos[nome] = consulta.contrato_consulta(wsdl, operacao)

    def chamar(self, servico, dados):
        nome, _, versao = SERVICOS[servico]
        caminho = "/gnreWS/services/" + nome
        self.preparar(servico)
        namespace, acao = self.contratos[nome]
        return consulta.requisitar(
            self.contexto, self.host, "POST", caminho, envelope(namespace, versao, dados),
            {"Content-Type": f'application/soap+xml; charset=utf-8; action="{acao}"'},
            timeout=self.timeout, tentativas=1 if servico == "recepcao" else 2,
            etapa="recepção do lote" if servico == "recepcao" else "consulta do recibo",
            limite_resposta=LIMITE_RESULTADO,
        )

    def configuracao(self, uf, receita):
        wsdl = consulta.requisitar(
            self.contexto, self.host, "GET", consulta.SERVICO + "?wsdl", timeout=self.timeout,
        )
        namespace, acao = consulta.contrato_consulta(wsdl)
        retorno = consulta.requisitar(
            self.contexto, self.host, "POST", consulta.SERVICO,
            consulta.montar_consulta(namespace, self.ambiente, uf, receita),
            {"Content-Type": f'application/soap+xml; charset=utf-8; action="{acao}"'}, timeout=self.timeout,
        )
        return consulta.obter_configuracao(retorno, namespace, self.ambiente, uf)


def identidade_guia(guia):
    if guia.get("versao") != "2.00" or texto(guia, "tipoGnre") != "0":
        raise RuntimeError("O fluxo inicial aceita guias simples na versão 2.00.")
    cnpj = guia.findtext(f"{{{GNRE}}}contribuinteEmitente/{{{GNRE}}}identificacao/{{{GNRE}}}CNPJ")
    itens = guia.findall(f"{{{GNRE}}}itensGNRE/{{{GNRE}}}item")
    if not cnpj or len(itens) != 1:
        raise RuntimeError("Cada guia deve identificar o emitente por CNPJ e ter um item.")
    item = itens[0]
    documento = item.find(f"{{{GNRE}}}documentoOrigem")
    identificador = texto(guia, "identificadorGuia")
    receita = texto(item, "receita")
    if documento is None or not documento.text:
        raise RuntimeError("Cada guia deve referenciar uma NF-e por seu documento de origem.")
    chave = documento.text if documento.get("tipo") in DOCUMENTOS_CHAVE_NFE else ""
    por_numero = DOCUMENTOS_NUMERO_NFE.get((texto(guia, "ufFavorecida"), receita))
    if por_numero and documento.get("tipo") == por_numero[0]:
        campos = [
            campo for campo in item.findall(f"{{{GNRE}}}camposExtras/{{{GNRE}}}campoExtra")
            if texto(campo, "codigo") == por_numero[1]
        ]
        if len(campos) != 1:
            raise RuntimeError("A guia deve informar uma única chave de NF-e no campo adicional 66.")
        chave = texto(campos[0], "valor")
    if not re.fullmatch(r"[0-9]{6}[A-Z0-9]{12}[0-9]{26}", chave) or chave[20:22] != "55":
        raise RuntimeError("A guia não contém uma chave de NF-e válida no documento ou campo adicional mapeado.")
    if documento.get("tipo") not in DOCUMENTOS_CHAVE_NFE and (
        not re.fullmatch(r"[0-9]{1,9}", documento.text) or int(documento.text) <= 0
        or int(documento.text) != int(chave[25:34])
    ):
        raise RuntimeError("O número do documento de origem difere da chave de acesso da NF-e.")
    if chave[6:20] != cnpj:
        raise RuntimeError("O CNPJ da guia difere da chave da NF-e referenciada.")
    if receita not in RECEITAS.values() or not identificador:
        raise RuntimeError("Receita ou identificador de guia incompatível com este fluxo.")
    identidade = "|".join((cnpj, texto(guia, "ufFavorecida"), receita, chave))
    return cnpj, identificador, identidade


def identificar_lote(conteudo):
    raiz = validar_xsd(conteudo)
    guias = raiz.findall(f"{{{GNRE}}}guias/{{{GNRE}}}TDadosGNRE")
    if not guias or len(conteudo) > 600 * 1024:
        raise RuntimeError("Lote vazio ou maior que 600 KB.")
    origens = {}
    cnpjs = set()
    for guia in guias:
        cnpj, identificador, identidade = identidade_guia(guia)
        if identificador in origens:
            raise RuntimeError("Identificador de guia repetido no lote.")
        origens[identificador] = identidade
        cnpjs.add(cnpj)
    if len(cnpjs) != 1 or len(set(origens.values())) != len(origens):
        raise RuntimeError("O lote deve conter um emitente e guias sem repetição de nota/receita.")
    return next(iter(cnpjs)), origens


def abrir_registro(pasta):
    pasta.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(pasta / "controle.sqlite3", timeout=10)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys=ON")
    conexao.executescript("""
        CREATE TABLE IF NOT EXISTS lotes (
            id TEXT PRIMARY KEY, ambiente TEXT NOT NULL, xml BLOB NOT NULL,
            origens TEXT NOT NULL, pasta TEXT NOT NULL, estado TEXT NOT NULL,
            recibo TEXT, recepcao BLOB, resultado BLOB, erro TEXT,
            proxima_consulta REAL NOT NULL DEFAULT 0,
            consultando_ate REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS guias (
            ambiente TEXT NOT NULL, identidade TEXT NOT NULL,
            lote TEXT NOT NULL REFERENCES lotes(id),
            PRIMARY KEY (ambiente, identidade)
        );
    """)
    return conexao


def registrar_lote(conexao, pasta, ambiente, conteudo):
    cnpj, origens = identificar_lote(conteudo)
    canonico = etree.tostring(validar_xsd(conteudo), method="c14n")
    lote_id = hashlib.sha256(ambiente.encode() + b"\0" + canonico).hexdigest()
    destino = pasta / ambiente / cnpj / date.today().isoformat() / lote_id
    with conexao:
        conexao.execute("BEGIN IMMEDIATE")
        existente = conexao.execute("SELECT * FROM lotes WHERE id=?", (lote_id,)).fetchone()
        if existente is not None:
            return existente
        for identidade in origens.values():
            anterior = conexao.execute(
                "SELECT lote FROM guias WHERE ambiente=? AND identidade=?", (ambiente, identidade),
            ).fetchone()
            if anterior is not None:
                raise RuntimeError(f"Já existe envio registrado para esta nota e receita no lote {anterior['lote']}.")
        conexao.execute(
            "INSERT INTO lotes(id,ambiente,xml,origens,pasta,estado) VALUES(?,?,?,?,?,'preparado')",
            (lote_id, ambiente, conteudo, json.dumps(origens), str(destino)),
        )
        conexao.executemany(
            "INSERT INTO guias(ambiente,identidade,lote) VALUES(?,?,?)",
            ((ambiente, identidade, lote_id) for identidade in origens.values()),
        )
    gravar(destino / "lote.xml", conteudo)
    return conexao.execute("SELECT * FROM lotes WHERE id=?", (lote_id,)).fetchone()


def processar_recepcao(conexao, lote, resposta, ambiente):
    retorno = extrair_retorno(resposta, "TRetLote_GNRE")
    if texto(retorno, "ambiente") != ambiente:
        raise RuntimeError("A resposta da recepção pertence a outro ambiente.")
    situacao = retorno.find(f"{{{GNRE}}}situacaoRecepcao")
    if situacao is None:
        raise RuntimeError("A recepção não informou a situação do lote.")
    codigo, descricao = texto(situacao, "codigo"), texto(situacao, "descricao")
    if codigo != "100":
        with conexao:
            conexao.execute(
                "UPDATE lotes SET estado='rejeitado',erro=? WHERE id=?", (f"{codigo}: {descricao}", lote["id"]),
            )
            conexao.execute("DELETE FROM guias WHERE lote=?", (lote["id"],))
        raise RuntimeError(f"Lote rejeitado na recepção ({codigo}): {descricao}")
    numero = retorno.findtext(f"{{{GNRE}}}recibo/{{{GNRE}}}numero", default="")
    if not re.fullmatch(r"(?:[0-9]{10}|[0-9]{14})", numero):
        raise RuntimeError("Número de recibo ausente ou inválido.")
    with conexao:
        conexao.execute(
            "UPDATE lotes SET recibo=?,estado='recebido',proxima_consulta=? WHERE id=?",
            (numero, time.time() + 60, lote["id"]),
        )
    print(f"Lote recebido. Recibo {numero}.", flush=True)
    return numero


def receber(conexao, lote, servico):
    if lote["recibo"]:
        return lote["recibo"]
    if lote["recepcao"] and lote["estado"] in ("enviando", "indeterminado"):
        return processar_recepcao(conexao, lote, lote["recepcao"], servico.ambiente)
    gravar(Path(lote["pasta"]) / "lote.xml", lote["xml"])
    servico.preparar("recepcao")
    with conexao:
        alterados = conexao.execute(
            "UPDATE lotes SET estado='enviando' WHERE id=? AND estado='preparado'", (lote["id"],),
        ).rowcount
    if not alterados:
        raise RuntimeError("O lote já teve uma tentativa de envio. Confira seu registro antes de transmitir novamente.")
    try:
        resposta = servico.chamar("recepcao", lote["xml"])
        with conexao:
            conexao.execute("UPDATE lotes SET recepcao=? WHERE id=?", (resposta, lote["id"]))
        gravar(Path(lote["pasta"]) / "recepcao.xml", resposta)
        return processar_recepcao(conexao, lote, resposta, servico.ambiente)
    except Exception as erro:
        with conexao:
            conexao.execute(
                "UPDATE lotes SET estado='indeterminado',erro=? WHERE id=? AND estado='enviando'",
                (str(erro), lote["id"]),
            )
        raise


def consultar_lote(conexao, lote, servico):
    if not lote["recibo"]:
        raise RuntimeError("O lote não possui recibo. Confira a resposta de recepção antes de continuar.")
    restante = lote["proxima_consulta"] - time.time()
    if restante > 0:
        print(f"Aguardando o intervalo de consulta ({restante:.0f}s).", flush=True)
        return False
    servico.preparar("resultado")
    with conexao:
        # A reserva do intervalo evita consultas concorrentes ao mesmo recibo.
        if not conexao.execute(
            "UPDATE lotes SET proxima_consulta=?,consultando_ate=? "
            "WHERE id=? AND proxima_consulta<=? AND consultando_ate<=?",
            (time.time() + 60, time.time() + servico.timeout * 2 + 20, lote["id"], time.time(), time.time()),
        ).rowcount:
            return False
    dados = etree.Element(f"{{{GNRE}}}TConsLote_GNRE", nsmap={None: GNRE})
    for nome, valor in (("ambiente", servico.ambiente), ("numeroRecibo", lote["recibo"]), ("incluirPDFGuias", "S")):
        etree.SubElement(dados, f"{{{GNRE}}}{nome}").text = valor
    pedido = etree.tostring(dados, encoding="utf-8")
    validar_xsd(pedido, "lote_gnre_consulta_v1.00.xsd")
    try:
        resposta = servico.chamar("resultado", pedido)
        return processar_resultado(conexao, lote, resposta, servico.ambiente)
    finally:
        with conexao:
            conexao.execute("UPDATE lotes SET consultando_ate=0 WHERE id=?", (lote["id"],))


def processar_resultado(conexao, lote, resposta, ambiente):
    with conexao:
        conexao.execute("UPDATE lotes SET resultado=? WHERE id=?", (resposta, lote["id"]))
    pasta = Path(lote["pasta"])
    gravar(pasta / "resultado.xml", resposta)
    retorno = extrair_retorno(resposta, "TResultLote_GNRE")
    validar_xsd(ET.tostring(retorno, encoding="utf-8"), "lote_gnre_result_v2.00.xsd")
    if texto(retorno, "ambiente") != ambiente or texto(retorno, "numeroRecibo") != lote["recibo"]:
        raise RuntimeError("O retorno pertence a outro recibo ou ambiente.")
    situacao = retorno.find(f"{{{GNRE}}}situacaoProcess")
    if situacao is None:
        raise RuntimeError("Retorno sem situação do processamento.")
    codigo = texto(situacao, "codigo")
    print(f"Processamento {codigo}. {texto(situacao, 'descricao')}", flush=True)
    if codigo in ("400", "401"):
        return False
    if codigo not in ("402", "403"):
        raise RuntimeError("O lote não retornou um resultado de processamento válido.")
    resultado = retorno.find(f"{{{GNRE}}}resultado")
    if resultado is None:
        raise RuntimeError("O retorno não contém o resultado das guias.")
    esperadas = json.loads(lote["origens"])
    guias = []
    for guia in resultado.findall(f"{{{GNRE}}}guia"):
        _, identificador, origem_retornada = identidade_guia(guia)
        if identificador not in esperadas or origem_retornada != esperadas[identificador]:
            raise RuntimeError("O resultado contém uma guia diferente do lote enviado.")
        motivos = [
            {"codigo": texto(m, "codigo"), "descricao": texto(m, "descricao"), "campo": texto(m, "campo")}
            for m in guia.findall(f"{{{GNRE}}}motivosRejeicao/{{{GNRE}}}motivo")
        ]
        guias.append({
            "identificador": identificador, "situacao": texto(guia, "situacaoGuia"),
            "linha_digitavel": texto(guia, "linhaDigitavel"), "codigo_barras": texto(guia, "codigoBarras"),
            "motivos": motivos,
        })
    if len(guias) != len(esperadas) or {g["identificador"] for g in guias} != set(esperadas):
        raise RuntimeError("A quantidade ou a identificação das guias retornadas difere do lote enviado.")
    pdf_texto = texto(resultado, "pdfGuias")
    if pdf_texto:
        try:
            pdf = base64.b64decode("".join(pdf_texto.split()), validate=True)
        except ValueError as erro:
            raise RuntimeError("O PDF retornado está com Base64 inválido.") from erro
        if not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-2048:]:
            raise RuntimeError("O arquivo retornado não contém um PDF completo.")
        gravar(pasta / f"guias-{lote['recibo']}.pdf", pdf)
    pendente = any(g["situacao"] in ("3", "4") for g in guias)
    if any(g["situacao"] not in ("0", "1", "2", "3", "4") for g in guias):
        raise RuntimeError("O retorno contém situação de guia desconhecida.")
    aceitas = sum(g["situacao"] == "0" for g in guias)
    if aceitas and not pdf_texto:
        raise RuntimeError("Há guias aceitas, mas o PDF solicitado não foi retornado. Consulte o recibo novamente.")
    resumo = {"ambiente": lote["ambiente"], "recibo": lote["recibo"], "guias": guias}
    gravar(pasta / "resumo.json", (json.dumps(resumo, ensure_ascii=False, indent=2) + "\n").encode())
    estado = "pendente" if pendente else ("concluido" if aceitas == len(guias) else "com_rejeicoes")
    with conexao:
        conexao.execute("UPDATE lotes SET estado=? WHERE id=?", (estado, lote["id"]))
    print(f"Guias aceitas: {aceitas}/{len(guias)}. Arquivos em {pasta}.", flush=True)
    return not pendente


def acompanhar(conexao, lote_id, servico, aguardar):
    limite = time.monotonic() + aguardar
    while True:
        lote = conexao.execute("SELECT * FROM lotes WHERE id=?", (lote_id,)).fetchone()
        if lote is None:
            raise RuntimeError("Lote não encontrado no registro.")
        if lote["estado"] in ("concluido", "com_rejeicoes"):
            print(f"Lote {lote['estado']}. Arquivos em {lote['pasta']}.")
            return 0 if lote["estado"] == "concluido" else 1
        if consultar_lote(conexao, lote, servico):
            continue
        intervalos = conexao.execute(
            "SELECT proxima_consulta,consultando_ate FROM lotes WHERE id=?", (lote_id,),
        ).fetchone()
        espera = max(1, max(intervalos) - time.time())
        if time.monotonic() + espera > limite:
            print(f"Consulta pendente. Para retomar, consulte o lote {lote_id}.")
            return 2
        time.sleep(espera)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identidade", type=Path, default=Path("/run/gnre/identidade.pem"))
    parser.add_argument("--dados", type=Path, default=Path("/dados/gnre"))
    parser.add_argument("--ambiente", choices=consulta.AMBIENTES, default="homologacao")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--confirmar-producao", action="store_true")
    comandos = parser.add_subparsers(dest="comando", required=True)
    for nome in ("preparar", "executar"):
        comando = comandos.add_parser(nome)
        comando.add_argument("--xml", type=Path, required=True)
        comando.add_argument("--vencimento", type=date.fromisoformat, required=True)
        comando.add_argument("--pagamento", type=date.fromisoformat, required=True)
        if nome == "executar":
            comando.add_argument("--aguardar", type=int, default=300)
    enviar = comandos.add_parser("enviar")
    enviar.add_argument("--lote", type=Path, required=True)
    enviar.add_argument("--aguardar", type=int, default=300)
    consultar = comandos.add_parser("consultar")
    consultar.add_argument("--lote-id", required=True)
    consultar.add_argument("--aguardar", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300 or getattr(args, "aguardar", 0) < 0:
        parser.error("timeout ou tempo de acompanhamento inválido")
    if args.comando in ("enviar", "executar") and args.ambiente == "producao" and not args.confirmar_producao:
        parser.error("confirme explicitamente a emissão em produção com --confirmar-producao")
    try:
        args.dados = args.dados.expanduser().resolve()
        servico = ServicoGnre(args.identidade, args.ambiente, args.timeout)
        print(f"Ambiente de {args.ambiente}.", flush=True)
        if args.comando in ("preparar", "executar"):
            nota = ler_nfe(args.xml)
            configuracoes = {}
            for imposto, valor in nota["valores"].items():
                if valor > 0:
                    receita = RECEITAS[imposto]
                    configuracoes[receita] = servico.configuracao(nota["uf"], receita)
                    caminho = args.dados / args.ambiente / "configuracoes" / f"{nota['uf']}-{receita}.xml"
                    gravar(caminho, ET.tostring(configuracoes[receita], encoding="utf-8"))
            lote = montar_lote(nota, configuracoes, args.vencimento, args.pagamento)
            pasta = args.dados / args.ambiente / "preparados" / hashlib.sha256(lote).hexdigest()
            gravar(pasta / "lote.xml", lote)
            for receita, configuracao in configuracoes.items():
                gravar(pasta / f"config-{receita}.xml", ET.tostring(configuracao, encoding="utf-8"))
            print(f"Lote preparado e validado por XSD. Arquivo {pasta / 'lote.xml'}")
            if args.comando == "preparar":
                return 0
            conteudo = lote
        elif args.comando == "enviar":
            conteudo = args.lote.read_bytes()
        with closing(abrir_registro(args.dados)) as conexao:
            if args.comando in ("enviar", "executar"):
                lote = registrar_lote(conexao, args.dados, args.ambiente, conteudo)
                print(f"Identificador do lote: {lote['id']}", flush=True)
                receber(conexao, lote, servico)
                lote_id = lote["id"]
            else:
                lote_id = args.lote_id
                existente = conexao.execute("SELECT ambiente FROM lotes WHERE id=?", (lote_id,)).fetchone()
                if existente is None or existente["ambiente"] != args.ambiente:
                    raise RuntimeError("Lote ausente ou pertencente a outro ambiente.")
            return acompanhar(conexao, lote_id, servico, args.aguardar)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, etree.LxmlError, http.client.HTTPException) as erro:
        print(f"Falha GNRE: {erro}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
