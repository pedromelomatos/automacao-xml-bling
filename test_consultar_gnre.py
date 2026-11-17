import unittest
from contextlib import redirect_stdout
from io import StringIO
import json
from unittest.mock import MagicMock, patch
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
    def test_retry_after_segundos_ou_data_http(self):
        self.assertEqual(gnre.espera_http('900'), 900)
        self.assertEqual(gnre.espera_http(None), 0)
        self.assertEqual(gnre.espera_http('invalido'), 0)
        with patch.object(gnre.time, 'time', return_value=0):
            self.assertEqual(gnre.espera_http('Thu, 01 Jan 1970 00:15:00 GMT'), 900)

    def test_consulta_usa_contrato_wsdl_e_somente_dados_da_uf(self):
        namespace, acao = gnre.contrato_consulta(CONTRATO)
        self.assertEqual(acao, NAMESPACE + "/consultar")
        xml = ET.fromstring(gnre.montar_consulta(namespace, "1", "MS", "100102"))
        consulta = xml.find(f".//{{{gnre.GNRE}}}TConsultaConfigUf")
        self.assertEqual([elemento.text for elemento in consulta], ["1", "MS", "100102"])
        self.assertEqual(xml.findtext(f".//{{{namespace}}}versaoDados"), "1.00")

    def test_dados_da_consulta_usam_namespace_padrao_sem_prefixos(self):
        xml = gnre.montar_consulta(NAMESPACE, "1", "MS", "100102")
        self.assertIn(f'<TConsultaConfigUf xmlns="{gnre.GNRE}">'.encode(), xml)
        self.assertIn(b"<ambiente>1</ambiente><uf>MS</uf><receita>100102</receita>", xml)
        self.assertIn(f'<gnreCabecMsg xmlns="{NAMESPACE}"><versaoDados>1.00</versaoDados>'.encode(), xml)

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

    def test_timeout_na_leitura_abre_nova_conexao_e_repete_a_consulta(self):
        primeira, segunda = MagicMock(), MagicMock()
        primeira.getresponse.return_value.__enter__.return_value.read.side_effect = TimeoutError()
        resposta_final = segunda.getresponse.return_value.__enter__.return_value
        resposta_final.status = 200
        resposta_final.read.return_value = b"<resposta/>"
        contexto = object()
        with patch.object(gnre.http.client, "HTTPSConnection", side_effect=[primeira, segunda]) as conexoes:
            resultado = gnre.requisitar(
                contexto, "gnre.example", "POST", gnre.SERVICO,
                b"<consulta/>", {"Content-Type": "application/soap+xml"}, timeout=90,
            )
        self.assertEqual(resultado, b"<resposta/>")
        self.assertEqual(conexoes.call_count, 2)
        conexoes.assert_called_with("gnre.example", context=contexto, timeout=90)
        primeira.close.assert_called_once()
        segunda.close.assert_called_once()
        self.assertEqual(primeira.request.call_args, segunda.request.call_args)

    def test_timeouts_persistentes_informam_a_etapa_e_encerram(self):
        for metodo, caminho, etapa in [
            ("GET", gnre.SERVICO + "?wsdl", "leitura do WSDL"),
            ("POST", gnre.SERVICO, "consulta das configurações da UF"),
        ]:
            with self.subTest(metodo=metodo):
                conexao = MagicMock()
                conexao.request.side_effect = TimeoutError()
                with patch.object(gnre.http.client, "HTTPSConnection", return_value=conexao) as conexoes:
                    with self.assertRaisesRegex(RuntimeError, etapa):
                        gnre.requisitar(object(), "gnre.example", metodo, caminho)
                self.assertEqual(conexoes.call_count, 2)
                self.assertEqual(conexao.close.call_count, 2)

    def test_erro_http_nao_e_repetido_como_timeout(self):
        conexao = MagicMock()
        resposta_http = conexao.getresponse.return_value.__enter__.return_value
        resposta_http.status = 403
        resposta_http.read.return_value = b"Forbidden"
        with patch.object(gnre.http.client, "HTTPSConnection", return_value=conexao) as conexoes:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                gnre.requisitar(object(), "gnre.example", "GET", gnre.SERVICO + "?wsdl")
        self.assertEqual(conexoes.call_count, 1)

    def test_erros_de_gateway_repetem_a_consulta_em_nova_conexao(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                primeira, segunda = MagicMock(), MagicMock()
                primeira_resposta = primeira.getresponse.return_value.__enter__.return_value
                primeira_resposta.status = status
                primeira_resposta.read.return_value = b"Bad Gateway"
                segunda_resposta = segunda.getresponse.return_value.__enter__.return_value
                segunda_resposta.status = 200
                segunda_resposta.read.return_value = b"<resposta/>"
                with patch.object(gnre.http.client, "HTTPSConnection", side_effect=[primeira, segunda]), patch.object(
                    gnre.time, "sleep"
                ) as esperar:
                    self.assertEqual(
                        gnre.requisitar(object(), "gnre.example", "POST", gnre.SERVICO),
                        b"<resposta/>",
                    )
                esperar.assert_called_once_with(2)
                primeira.close.assert_called_once()
                segunda.close.assert_called_once()

    def test_erro_de_gateway_persistente_encerra_apos_duas_tentativas(self):
        conexao = MagicMock()
        resposta_http = conexao.getresponse.return_value.__enter__.return_value
        resposta_http.status = 502
        resposta_http.read.return_value = b"Bad Gateway"
        with patch.object(gnre.http.client, "HTTPSConnection", return_value=conexao) as conexoes, patch.object(
            gnre.time, "sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502.*tentativa 2/2"):
                gnre.requisitar(object(), "gnre.example", "POST", gnre.SERVICO)
        self.assertEqual(conexoes.call_count, 2)

    def test_diagnostico_mostra_erro_limitado_e_cabecalhos_selecionados(self):
        conexao = MagicMock()
        resposta_http = conexao.getresponse.return_value.__enter__.return_value
        resposta_http.status = 502
        resposta_http.read.return_value = b"Bad Gateway" + b"x" * 3000
        resposta_http.getheader.side_effect = lambda nome: "nginx" if nome == "Server" else None
        saida = StringIO()
        with patch.object(gnre.http.client, "HTTPSConnection", return_value=conexao), patch.object(
            gnre.time, "sleep"
        ), redirect_stdout(saida):
            with self.assertRaises(RuntimeError):
                gnre.requisitar(object(), "gnre.example", "POST", gnre.SERVICO, diagnostico=True)
        registros = [
            json.loads(linha.removeprefix("Diagnóstico HTTP "))
            for linha in saida.getvalue().splitlines() if linha.startswith("Diagnóstico HTTP ")
        ]
        self.assertEqual(len(registros), 2)
        self.assertEqual(registros[0]["status"], 502)
        self.assertEqual(registros[0]["cabecalhos"]["Server"], "nginx")
        self.assertEqual(len(registros[0]["corpo"]), 2048)
        self.assertNotIn("Set-Cookie", registros[0]["cabecalhos"])

    def test_diagnostico_wsdl_preserva_endereco_publicado_pelo_servidor(self):
        conteudo = CONTRATO.replace(
            b"</definitions>",
            b'<service><port name="Soap12" binding="tns:Soap12">'
            b'<s:address location="https://gnre.example:444/consulta"/>'
            b"</port></service></definitions>",
        )
        saida = StringIO()
        with redirect_stdout(saida):
            gnre.diagnosticar_contrato(conteudo, NAMESPACE, NAMESPACE + "/consultar")
        registro = json.loads(saida.getvalue().removeprefix("Diagnóstico WSDL "))
        self.assertEqual(registro["portas"][0]["url"], "https://gnre.example:444/consulta")

    def test_transmissao_sem_repeticao_encerra_no_primeiro_502_ou_timeout(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                conexao = MagicMock()
                if timeout:
                    conexao.request.side_effect = TimeoutError()
                resposta_http = conexao.getresponse.return_value.__enter__.return_value
                resposta_http.status = 502
                resposta_http.read.return_value = b"Bad Gateway"
                with patch.object(gnre.http.client, "HTTPSConnection", return_value=conexao) as conexoes:
                    with self.assertRaises(RuntimeError):
                        gnre.requisitar(
                            object(), "gnre.example", "POST", "/recepcao", tentativas=1, etapa="recepção do lote",
                        )
                self.assertEqual(conexoes.call_count, 1)


if __name__ == "__main__":
    unittest.main()
