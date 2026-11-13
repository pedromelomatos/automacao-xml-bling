"""Prepara GNRE simples de DIFAL e FCP com valores de uma NF-e autorizada."""

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import unicodedata

from lxml import etree

from consultar_gnre import GNRE, carregar_xml
from organizar_xmls import nome_local, primeiro_texto


SCHEMAS = Path(__file__).resolve().parent / "schemas" / "gnre"
RECEITAS = {"DIFAL": "100102", "FCP": "100129"}
DOCUMENTOS_CHAVE_NFE = {"22", "24"}
# Paraná: número da NF-e no tipo 10 e chave de acesso no campo extra 66.
DOCUMENTOS_NUMERO_NFE = {("PR", "100102"): ("10", "66")}
PRODUTOS = (
    "Materiais de Construção, Acabamentos, Bricolagens ou Adornos",
    "Material Elétrico",
)


def texto(elemento, nome, padrao=""):
    for filho in elemento:
        if nome_local(filho).lower() == nome.lower():
            return (filho.text or "").strip()
    return padrao


def dinheiro(valor):
    try:
        numero = Decimal(valor)
        if not numero.is_finite() or numero < 0 or numero != numero.quantize(Decimal("0.01")):
            raise ValueError()
        return numero
    except (InvalidOperation, TypeError, ValueError) as erro:
        raise RuntimeError("O XML contém um valor de imposto inválido.") from erro


def ler_nfe(caminho):
    raiz = carregar_xml(Path(caminho).read_bytes())
    notas = [elemento for elemento in raiz.iter() if nome_local(elemento) == "infNFe"]
    protocolos = [elemento for elemento in raiz.iter() if nome_local(elemento) == "infProt"]
    if len(notas) != 1 or len(protocolos) != 1:
        raise RuntimeError("Informe o XML de uma NF-e autorizada, incluindo seu protocolo.")
    nota, protocolo = notas[0], protocolos[0]
    if texto(protocolo, "cStat") not in ("100", "150"):
        raise RuntimeError("O protocolo da NF-e não informa autorização.")
    chave = texto(protocolo, "chNFe")
    if not re.fullmatch(r"[0-9]{6}[A-Z0-9]{12}[0-9]{26}", chave):
        raise RuntimeError("Chave de acesso da NF-e inválida.")
    if nota.get("Id") != "NFe" + chave:
        raise RuntimeError("A chave da NF-e difere da chave no protocolo.")
    cnpj = primeiro_texto(nota, ("emit", "CNPJ"))
    if cnpj != chave[6:20]:
        raise RuntimeError("O CNPJ do emitente difere da chave da NF-e.")
    if primeiro_texto(nota, ("ide", "mod")) != "55":
        raise RuntimeError("O documento de origem deve ser uma NF-e modelo 55.")
    if primeiro_texto(nota, ("ide", "indFinal")) != "1" or primeiro_texto(nota, ("dest", "indIEDest")) != "9":
        raise RuntimeError("Este fluxo atende DIFAL/FCP de consumidor final não contribuinte.")
    uf = primeiro_texto(nota, ("dest", "enderDest", "UF"))
    if not uf or uf == primeiro_texto(nota, ("emit", "enderEmit", "UF")):
        raise RuntimeError("A NF-e deve conter uma operação interestadual.")
    emissao = primeiro_texto(nota, ("ide", "dhEmi")) or primeiro_texto(nota, ("ide", "dEmi"))
    try:
        data_emissao = date.fromisoformat(emissao[:10])
    except (TypeError, ValueError) as erro:
        raise RuntimeError("Data de emissão da NF-e inválida.") from erro
    for campo in ("vST", "vFCPST"):
        if dinheiro(primeiro_texto(nota, ("total", "ICMSTot", campo)) or "0") > 0:
            raise RuntimeError("A NF-e contém ICMS-ST. O fluxo inicial atende DIFAL e FCP do DIFAL.")
    valores = {}
    for imposto, campo in (("DIFAL", "vICMSUFDest"), ("FCP", "vFCPUFDest")):
        itens = [
            primeiro_texto(item, ("imposto", "ICMSUFDest", campo))
            for item in nota if nome_local(item) == "det"
        ]
        soma = sum((dinheiro(valor) for valor in itens if valor is not None), Decimal("0.00"))
        total = primeiro_texto(nota, ("total", "ICMSTot", campo))
        valores[imposto] = dinheiro(total) if total is not None else soma
        if any(valor is not None for valor in itens) and valores[imposto] != soma:
            raise RuntimeError(f"O total de {imposto} difere da soma dos itens da NF-e.")
    if not any(valores.values()):
        raise RuntimeError("A NF-e não contém valores de DIFAL ou FCP para gerar guia.")
    return {"xml": nota, "chave": chave, "cnpj": cnpj, "uf": uf, "emissao": data_emissao, "valores": valores}


def validar_xsd(conteudo, nome="lote_gnre_v2.00.xsd"):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    carregar_xml(conteudo)
    documento = etree.fromstring(conteudo, parser)
    schema = etree.XMLSchema(etree.parse(str(SCHEMAS / nome), parser))
    if not schema.validate(documento):
        erro = schema.error_log.last_error
        raise RuntimeError(f"XML GNRE incompatível com o XSD: {erro.message}")
    return documento


def normalizar(valor):
    sem_acentos = unicodedata.normalize("NFKD", valor)
    return "".join(caractere.lower() for caractere in sem_acentos if caractere.isalnum())


def adicionar(pai, nome, valor, **atributos):
    filho = etree.SubElement(pai, f"{{{GNRE}}}{nome}", **atributos)
    filho.text = str(valor)
    return filho


def contribuinte(pai, nome, nota, grupo):
    origem = next((filho for filho in nota if nome_local(filho) == grupo), None)
    if origem is None:
        raise RuntimeError(f"Dados do contribuinte ausentes na NF-e ({grupo}).")
    destino = adicionar(pai, nome, "")
    identificacao = adicionar(destino, "identificacao", "")
    tipo = "CNPJ" if texto(origem, "CNPJ") else "CPF"
    documento = texto(origem, tipo)
    if not documento:
        raise RuntimeError(f"Identificação do contribuinte ausente na NF-e ({grupo}).")
    adicionar(identificacao, tipo, documento)
    adicionar(destino, "razaoSocial", texto(origem, "xNome")[:60])
    endereco = next((filho for filho in origem if nome_local(filho) == "ender" + grupo.capitalize()), None)
    if endereco is None:
        raise RuntimeError(f"Endereço do contribuinte ausente na NF-e ({grupo}).")
    if grupo == "emit":
        logradouro = ", ".join(texto(endereco, campo) for campo in ("xLgr", "nro", "xCpl") if texto(endereco, campo))
        adicionar(destino, "endereco", logradouro[:60])
    municipio = texto(endereco, "cMun")
    if not re.fullmatch(r"[0-9]{7}", municipio):
        raise RuntimeError(f"Código de município inválido na NF-e ({grupo}).")
    adicionar(destino, "municipio", municipio[2:])
    if grupo == "emit":
        adicionar(destino, "uf", texto(endereco, "UF"))
        adicionar(destino, "cep", texto(endereco, "CEP"))
        fone = texto(endereco, "fone")
        if fone:
            adicionar(destino, "telefone", fone)


def regra_receita(configuracao, codigo):
    for elemento in configuracao.iter():
        if nome_local(elemento) == "receita" and elemento.get("codigo") == codigo:
            return elemento
    raise RuntimeError(f"A UF não retornou a receita {codigo}.")


def aceita_versao_xml(elemento, grupo, versao="2.00"):
    versoes = next((filho for filho in elemento if nome_local(filho) == grupo), None)
    return versoes is None or any(
        nome_local(filho) == "versao" and (filho.text or "").strip() == versao for filho in versoes
    )


def montar_guia(lote, nota, imposto, configuracao, vencimento, pagamento, identificador):
    codigo = RECEITAS[imposto]
    regra = regra_receita(configuracao, codigo)
    tipos = [texto_elemento.text for texto_elemento in regra.iter() if nome_local(texto_elemento) == "tipoGnre"]
    if tipos and "0" not in tipos:
        raise RuntimeError(f"A receita {codigo} não admite GNRE simples.")
    if texto(regra, "exigeValorFecp") == "S":
        raise RuntimeError("A receita exige FCP integrado. É necessário mapear essa configuração antes de enviar.")
    if texto(regra, "valorExigido") not in ("P", "A", "PO", "AO"):
        raise RuntimeError(f"A receita {codigo} exige outra forma de informar o valor. Revise a configuração da UF.")
    if texto(regra, "exigeConvenio") == "O" or texto(regra, "exigeParcela") == "S":
        raise RuntimeError(f"A receita {codigo} exige convênio ou parcela. Complete esses parâmetros antes do envio.")
    guia = adicionar(lote, "TDadosGNRE", "", versao="2.00")
    adicionar(guia, "ufFavorecida", nota["uf"])
    adicionar(guia, "tipoGnre", "0")
    contribuinte(guia, "contribuinteEmitente", nota["xml"], "emit")
    item = adicionar(adicionar(guia, "itensGNRE", ""), "item", "")
    adicionar(item, "receita", codigo)
    if texto(regra, "exigeDetalhamentoReceita") == "S":
        detalhes = [e for e in regra.iter() if nome_local(e) == "detalhamentoReceita"]
        preferido = "000005" if imposto == "DIFAL" else {"AL": "000079"}.get(nota["uf"])
        codigos = [texto(e, "codigo") for e in detalhes]
        if preferido in codigos:
            detalhamento = preferido
        elif len(codigos) == 1:
            detalhamento = codigos[0]
        else:
            raise RuntimeError(f"Detalhamento da receita {codigo} precisa ser parametrizado para {nota['uf']}.")
        adicionar(item, "detalhamentoReceita", detalhamento)
    if not aceita_versao_xml(regra, "versoesXmlDocOrigem"):
        raise RuntimeError(f"O documento de origem da receita {codigo} não está habilitado no XML 2.00.")
    documentos = [
        texto(e, "codigo") for e in regra.iter()
        if nome_local(e) == "tipoDocumentoOrigem"
        and texto(e, "codigo") in DOCUMENTOS_CHAVE_NFE
        and "chave" in normalizar(texto(e, "descricao"))
        and any(termo in normalizar(texto(e, "descricao")) for termo in ("nfe", "notafiscal", "dfe"))
    ]
    documento = nota["chave"]
    if not documentos and (nota["uf"], codigo) in DOCUMENTOS_NUMERO_NFE:
        tipo, campo_chave = DOCUMENTOS_NUMERO_NFE[(nota["uf"], codigo)]
        por_numero = [
            e for e in regra.iter() if nome_local(e) == "tipoDocumentoOrigem"
            and texto(e, "codigo") == tipo and normalizar(texto(e, "descricao")) == "notafiscal"
        ]
        campos_chave = [
            e for e in regra.iter() if nome_local(e) == "campoAdicional"
            and texto(e, "codigo") == campo_chave and texto(e, "obrigatorio") == "S"
            and aceita_versao_xml(e, "versoesXmlCampoAdicional")
            and "chave" in normalizar(texto(e, "titulo"))
            and "nfe" in normalizar(texto(e, "titulo"))
            and texto(e, "tipo") == "T" and texto(e, "tamanho") == "44"
        ]
        if len(por_numero) != 1 or len(campos_chave) != 1:
            raise RuntimeError("A configuração do Paraná deve permitir o tipo 10 e exigir a chave no campo 66.")
        numero = primeiro_texto(nota["xml"], ("ide", "nNF")) or ""
        if not re.fullmatch(r"[0-9]{1,9}", numero) or int(numero) <= 0 or int(numero) != int(nota["chave"][25:34]):
            raise RuntimeError("O número da NF-e difere do número informado na chave de acesso.")
        documentos = [tipo]
        documento = str(int(numero))
    if len(documentos) != 1:
        raise RuntimeError(f"A receita {codigo} não retornou um tipo único de documento para a chave da NF-e.")
    adicionar(item, "documentoOrigem", documento, tipo=documentos[0])
    if texto(regra, "exigeProduto") == "S":
        produtos = {
            normalizar(texto(e, "descricao")): texto(e, "codigo")
            for e in regra.iter() if nome_local(e) == "produto"
        }
        produto = next((produtos[normalizar(nome)] for nome in PRODUTOS if normalizar(nome) in produtos), None)
        if produto is None:
            raise RuntimeError(f"Produto GNRE precisa ser parametrizado para {nota['uf']} e receita {codigo}.")
        adicionar(item, "produto", produto)
    if texto(regra, "exigePeriodoReferencia") == "S":
        periodos = [texto(e, "codigo") for e in regra.iter() if nome_local(e) == "periodoApuracao"]
        if texto(regra, "exigePeriodoApuracao") == "S" and "0" not in periodos:
            raise RuntimeError(f"Período de apuração precisa ser parametrizado para a receita {codigo}.")
        referencia = adicionar(item, "referencia", "")
        adicionar(referencia, "periodo", "0")
        adicionar(referencia, "mes", nota["emissao"].strftime("%m"))
        adicionar(referencia, "ano", nota["emissao"].year)
    adicionar(item, "dataVencimento", vencimento.isoformat())
    adicionar(item, "valor", f"{nota['valores'][imposto]:.2f}", tipo="11")
    if texto(regra, "exigeContribuinteDestinatario") in ("S", "O"):
        contribuinte(item, "contribuinteDestinatario", nota["xml"], "dest")
    adicionais = []
    for campo in regra.iter():
        if nome_local(campo) != "campoAdicional" or texto(campo, "obrigatorio") != "S" or not aceita_versao_xml(
            campo, "versoesXmlCampoAdicional",
        ):
            continue
        titulo = normalizar(texto(campo, "titulo"))
        if "chave" in titulo and ("nfe" in titulo or "notafiscal" in titulo):
            valor = nota["chave"]
        elif "data" in titulo and ("emissao" in titulo or "emiss" in titulo):
            valor = nota["emissao"].isoformat()
        else:
            raise RuntimeError(f"Campo adicional obrigatório exige parametrização: {texto(campo, 'titulo')}.")
        adicionais.append((texto(campo, "codigo"), valor))
    if adicionais:
        extras = adicionar(item, "camposExtras", "")
        for codigo_extra, valor in adicionais:
            campo = adicionar(extras, "campoExtra", "")
            adicionar(campo, "codigo", codigo_extra)
            adicionar(campo, "valor", valor)
    adicionar(guia, "valorGNRE", f"{nota['valores'][imposto]:.2f}")
    adicionar(guia, "dataPagamento", pagamento.isoformat())
    adicionar(guia, "identificadorGuia", identificador)


def montar_lote(nota, configuracoes, vencimento, pagamento):
    raiz = etree.Element(f"{{{GNRE}}}TLote_GNRE", nsmap={None: GNRE}, versao="2.00")
    guias = adicionar(raiz, "guias", "")
    identificador = 0
    for imposto, valor in nota["valores"].items():
        if valor > 0:
            identificador += 1
            montar_guia(guias, nota, imposto, configuracoes[RECEITAS[imposto]], vencimento, pagamento, identificador)
    conteudo = etree.tostring(raiz, encoding="utf-8", xml_declaration=True)
    validar_xsd(conteudo)
    return conteudo
