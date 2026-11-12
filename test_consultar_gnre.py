import unittest
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import consultar_gnre as gnre


NAMESPACE = "http://www.gnre.pe.gov.br/webservice/GnreConfigUF"
CONTRATO = f"""<definitions xmlns="{gnre.WSDL}" xmlns:s="{gnre.WSDL_SOAP}"
targetNamespace="{NAMESPACE}">
  <binding><s:binding transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="consultar"><s:operation soapAction="{NAMESPACE}/consultar"/></operation>
  </binding>
</definitions>""".encode()


def resposta(codigo="450", ambiente="1", uf="MS", escapada=False):
    config = f"""<TConfigUf xmlns="{gnre.GNRE}">
      <ambiente>{ambiente}</ambiente><uf>{uf}</uf>
      <situacaoConsulta><codigo>{codigo}</codigo><descricao>Retorno da consulta</descricao></situacaoConsulta>
    </TConfigUf>"""
    if escapada:
        config = escape(config)
    return f"""<s:Envelope xmlns:s="{gnre.SOAP}"><s:Body>
      <gnreRespostaMsg xmlns="{NAMESPACE}">{config}</gnreRespostaMsg>
    </s:Body></s:Envelope>""".encode()


class ConsultaGnreTests(unittest.TestCase):
    def test_consulta_usa_contrato_wsdl_e_somente_dados_da_uf(self):
        namespace, acao = gnre.contrato_consulta(CONTRATO)
        self.assertEqual(acao, NAMESPACE + "/consultar")
        xml = ET.fromstring(gnre.montar_consulta(namespace, "1", "MS", "100102"))
        consulta = xml.find(f".//{{{gnre.GNRE}}}TConsultaConfigUf")
        self.assertEqual([elemento.text for elemento in consulta], ["1", "MS", "100102"])
        self.assertEqual(xml.findtext(f".//{{{namespace}}}versaoDados"), "1.00")

    def test_retorno_direto_ou_escapado(self):
        for escapada in (False, True):
            with self.subTest(escapada=escapada):
                self.assertEqual(
                    gnre.analisar_resposta(resposta(escapada=escapada), NAMESPACE, "1", "MS"),
                    "Retorno da consulta",
                )

    def test_http_200_sem_consulta_aceita_nao_confirma_conexao(self):
        exemplos = [
            b"<html>Autenticacao necessaria</html>",
            resposta(codigo="102"),
            resposta(ambiente="2"),
            resposta(uf="AL"),
            resposta().replace(b"<codigo>450</codigo>", b""),
        ]
        for exemplo in exemplos:
            with self.subTest(exemplo=exemplo):
                with self.assertRaises(RuntimeError):
                    gnre.analisar_resposta(exemplo, NAMESPACE, "1", "MS")

    def test_falha_soap_exibe_o_motivo(self):
        xml = f"""<s:Envelope xmlns:s="{gnre.SOAP}"><s:Body><s:Fault>
          <s:Reason><s:Text>Versao rejeitada</s:Text></s:Reason>
        </s:Fault></s:Body></s:Envelope>""".encode()
        with self.assertRaisesRegex(RuntimeError, "Versao rejeitada"):
            gnre.analisar_resposta(xml, NAMESPACE, "1", "MS")

    def test_wsdl_sem_operacao_de_consulta_e_rejeitado(self):
        with self.assertRaises(RuntimeError):
            gnre.contrato_consulta(CONTRATO.replace(b'name="consultar"', b'name="emitir"'))

    def test_rejeita_declaracao_de_entidade(self):
        with self.assertRaises(RuntimeError):
            gnre.carregar_xml(b'<!DOCTYPE x [<!ENTITY dado "abc">]><x>&dado;</x>')


if __name__ == "__main__":
    unittest.main()
