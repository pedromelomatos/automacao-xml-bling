from datetime import date
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from lxml import etree

import preparar_gnre as preparar


CHAVE = "35261000000000000000550050000037041531922135"
NFE = f"""<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">
<NFe><infNFe Id="NFe{CHAVE}">
<ide><mod>55</mod><nNF>3704</nNF><dhEmi>2026-10-29T08:00:00-03:00</dhEmi><indFinal>1</indFinal></ide>
<emit><CNPJ>00000000000000</CNPJ><xNome>Empresa de Exemplo</xNome>
<enderEmit><xLgr>Rua Exemplo</xLgr><nro>1</nro><cMun>3550308</cMun><UF>SP</UF><CEP>01000000</CEP><fone>11999999999</fone></enderEmit></emit>
<dest><CPF>00000000000</CPF><xNome>Destinatario de Exemplo</xNome>
<enderDest><cMun>2704302</cMun><UF>AL</UF></enderDest><indIEDest>9</indIEDest></dest>
<det><imposto><ICMSUFDest><vICMSUFDest>6.77</vICMSUFDest><vFCPUFDest>0.50</vFCPUFDest></ICMSUFDest></imposto></det>
<total><ICMSTot><vICMSUFDest>6.77</vICMSUFDest><vFCPUFDest>0.50</vFCPUFDest><vST>0.00</vST></ICMSTot></total>
</infNFe></NFe><protNFe><infProt><chNFe>{CHAVE}</chNFe><cStat>100</cStat></infProt></protNFe>
</nfeProc>""".encode()


def configuracao(receita):
    detalhamento = "000005" if receita == "100102" else "000079"
    return ET.fromstring(f"""<TConfigUf xmlns="{preparar.GNRE}">
      <receitas><receita codigo="{receita}">
        <valorExigido>P</valorExigido><exigeValorFecp>N</exigeValorFecp>
        <exigeDetalhamentoReceita>S</exigeDetalhamentoReceita>
        <detalhamentosReceita><detalhamentoReceita><codigo>{detalhamento}</codigo></detalhamentoReceita></detalhamentosReceita>
        <tiposDocumentosOrigem><tipoDocumentoOrigem><codigo>05</codigo><descricao>DMI</descricao></tipoDocumentoOrigem>
        <tipoDocumentoOrigem><codigo>22</codigo><descricao>CHAVE DA NFe</descricao></tipoDocumentoOrigem>
        <tipoDocumentoOrigem><codigo>25</codigo><descricao>DUIMP</descricao></tipoDocumentoOrigem></tiposDocumentosOrigem>
        <exigeProduto>S</exigeProduto><produtos><produto><codigo>33</codigo>
        <descricao>{preparar.PRODUTOS[0]}</descricao></produto></produtos>
        <exigeContribuinteDestinatario>S</exigeContribuinteDestinatario>
      </receita></receitas>
    </TConfigUf>""")


def preparar_lote(pasta, conteudo=NFE, vencimento=date(2026, 11, 12)):
    arquivo = Path(pasta) / "nota.xml"
    arquivo.write_bytes(conteudo)
    nota = preparar.ler_nfe(arquivo)
    configuracoes = {receita: configuracao(receita) for receita in preparar.RECEITAS.values()}
    return preparar.montar_lote(nota, configuracoes, vencimento, vencimento)


def configuracao_pr():
    # Catálogo de PR/100102 retornado pelo GnreConfigUF em homologação.
    return ET.fromstring(f"""<TConfigUf xmlns="{preparar.GNRE}">
      <receitas><receita codigo="100102">
        <valorExigido>P</valorExigido><exigeValorFecp>N</exigeValorFecp>
        <exigeDetalhamentoReceita>N</exigeDetalhamentoReceita><exigeProduto>N</exigeProduto>
        <exigePeriodoReferencia>N</exigePeriodoReferencia><exigeParcela>N</exigeParcela>
        <exigeConvenio>S</exigeConvenio><exigeContribuinteDestinatario>S</exigeContribuinteDestinatario>
        <tiposDocumentosOrigem>
          <tipoDocumentoOrigem><codigo>10</codigo><descricao>NOTA FISCAL</descricao></tipoDocumentoOrigem>
          <tipoDocumentoOrigem><codigo>21</codigo><descricao>CONHECIMENTO DE TRANSPORTE</descricao></tipoDocumentoOrigem>
        </tiposDocumentosOrigem>
        <camposAdicionais><campoAdicional><obrigatorio>S</obrigatorio><codigo>66</codigo>
          <tipo>T</tipo><tamanho>44</tamanho><titulo>Chave de Acesso da NFe ou do CTe/CTe-OS</titulo>
        </campoAdicional><campoAdicional><obrigatorio>N</obrigatorio><codigo>61</codigo>
          <tipo>T</tipo><tamanho>100</tamanho><titulo>Informações Complementares</titulo>
        </campoAdicional></camposAdicionais>
        <tiposGnre><tipoGnre>0</tipoGnre></tiposGnre>
      </receita></receitas></TConfigUf>""")


NFE_PR = NFE.replace(b"<UF>AL</UF>", b"<UF>PR</UF>").replace(b"2704302", b"4106902").replace(b">0.50<", b">0.00<")


def preparar_lote_pr(pasta):
    arquivo = Path(pasta) / "nota-pr.xml"
    arquivo.write_bytes(NFE_PR)
    return preparar.montar_lote(
        preparar.ler_nfe(arquivo), {"100102": configuracao_pr()}, date(2026, 11, 12), date(2026, 11, 12),
    )


NFE_SC = NFE.replace(b"<UF>AL</UF>", b"<UF>SC</UF>").replace(b"2704302", b"4208906").replace(b">6.77<", b">1.68<").replace(b">0.50<", b">0.00<")


def configuracao_sc():
    # SC/100102: documento DFe tipo 24; campo 96 somente no XML 1.00.
    return ET.fromstring(f"""<TConfigUf xmlns="{preparar.GNRE}"><receitas><receita codigo="100102">
      <valorExigido>P</valorExigido><exigeValorFecp>N</exigeValorFecp>
      <exigeDetalhamentoReceita>N</exigeDetalhamentoReceita><exigeProduto>N</exigeProduto>
      <exigePeriodoReferencia>N</exigePeriodoReferencia><exigeParcela>N</exigeParcela>
      <exigeConvenio>N</exigeConvenio><exigeContribuinteDestinatario>N</exigeContribuinteDestinatario>
      <tiposDocumentosOrigem><tipoDocumentoOrigem><codigo>24</codigo><descricao>CHAVE DO DFe</descricao></tipoDocumentoOrigem></tiposDocumentosOrigem>
      <versoesXmlDocOrigem><versao>2.00</versao></versoesXmlDocOrigem>
      <camposAdicionais><campoAdicional><obrigatorio>S</obrigatorio><codigo>96</codigo>
        <tipo>T</tipo><tamanho>44</tamanho><titulo>Chave de Acesso da NFe</titulo>
        <versoesXmlCampoAdicional><versao>1.00</versao></versoesXmlCampoAdicional>
      </campoAdicional></camposAdicionais>
      <tiposGnre><tipoGnre>0</tipoGnre><tipoGnre>1</tipoGnre><tipoGnre>2</tipoGnre></tiposGnre>
    </receita></receitas></TConfigUf>""")


def preparar_lote_sc(pasta, config=None):
    arquivo = Path(pasta) / "nota-sc.xml"
    arquivo.write_bytes(NFE_SC)
    return preparar.montar_lote(
        preparar.ler_nfe(arquivo), {"100102": configuracao_sc() if config is None else config},
        date(2026, 11, 13), date(2026, 11, 13),
    )


class PrepararGnreTests(unittest.TestCase):
    def test_sc_envia_chave_dfe_tipo24_sem_campo_da_versao_antiga(self):
        with tempfile.TemporaryDirectory() as pasta:
            raiz = etree.fromstring(preparar_lote_sc(pasta))
        ns = {"g": preparar.GNRE}
        self.assertEqual(raiz.xpath("//g:documentoOrigem/@tipo", namespaces=ns), ["24"])
        self.assertEqual(raiz.xpath("//g:documentoOrigem/text()", namespaces=ns), [CHAVE])
        self.assertEqual(raiz.xpath("//g:item/g:valor/text()", namespaces=ns), ["1.68"])
        self.assertFalse(raiz.xpath("//g:camposExtras | //g:contribuinteDestinatario", namespaces=ns))

    def test_campo_extra_obrigatorio_da_versao_atual_e_preenchido(self):
        config = configuracao_sc()
        config.find(f".//{{{preparar.GNRE}}}versoesXmlCampoAdicional/{{{preparar.GNRE}}}versao").text = "2.00"
        with tempfile.TemporaryDirectory() as pasta:
            raiz = etree.fromstring(preparar_lote_sc(pasta, config))
        ns = {"g": preparar.GNRE}
        self.assertEqual(raiz.xpath("//g:campoExtra/g:codigo/text()", namespaces=ns), ["96"])
        self.assertEqual(raiz.xpath("//g:campoExtra/g:valor/text()", namespaces=ns), [CHAVE])

    def test_recusa_documento_de_origem_sem_suporte_ao_xml_atual(self):
        config = configuracao_sc()
        config.find(f".//{{{preparar.GNRE}}}versoesXmlDocOrigem/{{{preparar.GNRE}}}versao").text = "1.00"
        with tempfile.TemporaryDirectory() as pasta, self.assertRaisesRegex(RuntimeError, "XML 2.00"):
            preparar_lote_sc(pasta, config)

    def test_parana_envia_numero_da_nota_e_chave_no_campo_66(self):
        with tempfile.TemporaryDirectory() as pasta:
            lote = preparar_lote_pr(pasta)
        raiz = etree.fromstring(lote)
        ns = {"g": preparar.GNRE}
        self.assertEqual(raiz.xpath("//g:documentoOrigem/@tipo", namespaces=ns), ["10"])
        self.assertEqual(raiz.xpath("//g:documentoOrigem/text()", namespaces=ns), ["3704"])
        self.assertEqual(raiz.xpath("//g:campoExtra/g:codigo/text()", namespaces=ns), ["66"])
        self.assertEqual(raiz.xpath("//g:campoExtra/g:valor/text()", namespaces=ns), [CHAVE])
        self.assertEqual(raiz.xpath("//g:item/g:valor/text()", namespaces=ns), ["6.77"])

    def test_parana_recusa_numero_divergente_ou_catalogo_sem_chave_obrigatoria(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "nota.xml"
            arquivo.write_bytes(NFE_PR.replace(b"<nNF>3704</nNF>", b"<nNF>3705</nNF>"))
            with self.assertRaisesRegex(RuntimeError, "número da NF-e"):
                preparar.montar_lote(preparar.ler_nfe(arquivo), {"100102": configuracao_pr()}, date.today(), date.today())
            arquivo.write_bytes(NFE_PR)
            configuracoes = {"100102": configuracao_pr()}
            configuracoes["100102"].find(f".//{{{preparar.GNRE}}}campoAdicional/{{{preparar.GNRE}}}obrigatorio").text = "N"
            with self.assertRaisesRegex(RuntimeError, "campo 66"):
                preparar.montar_lote(preparar.ler_nfe(arquivo), configuracoes, date.today(), date.today())

    def test_prepara_difal_e_fcp_separados_com_xsd_e_valores_da_nfe(self):
        with tempfile.TemporaryDirectory() as pasta:
            lote = preparar_lote(pasta)
        raiz = etree.fromstring(lote)
        guias = raiz.findall(f"{{{preparar.GNRE}}}guias/{{{preparar.GNRE}}}TDadosGNRE")
        self.assertEqual(len(guias), 2)
        ns = {"g": preparar.GNRE}
        self.assertEqual(raiz.xpath("//g:valor/text()", namespaces=ns), ["6.77", "0.50"])
        self.assertEqual(raiz.xpath("//g:detalhamentoReceita/text()", namespaces=ns), ["000005", "000079"])
        self.assertEqual(raiz.xpath("//g:documentoOrigem/text()", namespaces=ns), [CHAVE, CHAVE])
        self.assertEqual(raiz.xpath("//g:documentoOrigem/@tipo", namespaces=ns), ["22", "22"])
        self.assertEqual(raiz.xpath("//g:contribuinteEmitente/g:municipio/text()", namespaces=ns), ["50308"] * 2)
        self.assertNotIn(b"ns0:", lote)

    def test_recusa_total_divergente_falta_de_autorizacao_e_st(self):
        exemplos = [
            NFE.replace(b"<total><ICMSTot><vICMSUFDest>6.77", b"<total><ICMSTot><vICMSUFDest>7.77"),
            NFE.replace(b"<cStat>100</cStat>", b"<cStat>101</cStat>"),
            NFE.replace(b"<vST>0.00</vST>", b"<vST>1.00</vST>"),
        ]
        with tempfile.TemporaryDirectory() as pasta:
            for conteudo in exemplos:
                with self.subTest(conteudo=conteudo):
                    with self.assertRaises(RuntimeError):
                        preparar_lote(pasta, conteudo)

    def test_imposto_zero_nao_produz_guia(self):
        with tempfile.TemporaryDirectory() as pasta:
            lote = preparar_lote(pasta, NFE.replace(b">0.50<", b">0.00<"))
        self.assertEqual(len(etree.fromstring(lote).findall(f".//{{{preparar.GNRE}}}TDadosGNRE")), 1)

    def test_produto_e_campo_extra_desconhecidos_bloqueiam_preparacao(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "nota.xml"
            arquivo.write_bytes(NFE)
            nota = preparar.ler_nfe(arquivo)
            configuracoes = {receita: configuracao(receita) for receita in preparar.RECEITAS.values()}
            receita = configuracoes["100102"]
            produto = receita.find(f".//{{{preparar.GNRE}}}produto/{{{preparar.GNRE}}}descricao")
            produto.text = "Categoria desconhecida"
            with self.assertRaisesRegex(RuntimeError, "Produto"):
                preparar.montar_lote(nota, configuracoes, date.today(), date.today())
            produto.text = preparar.PRODUTOS[0]
            campos = ET.SubElement(receita.find(f".//{{{preparar.GNRE}}}receita"), f"{{{preparar.GNRE}}}camposAdicionais")
            extra = ET.SubElement(campos, f"{{{preparar.GNRE}}}campoAdicional")
            for nome, valor in (("obrigatorio", "S"), ("codigo", "99"), ("titulo", "Informação específica")):
                ET.SubElement(extra, f"{{{preparar.GNRE}}}{nome}").text = valor
            with self.assertRaisesRegex(RuntimeError, "Campo adicional"):
                preparar.montar_lote(nota, configuracoes, date.today(), date.today())


if __name__ == "__main__":
    unittest.main()
