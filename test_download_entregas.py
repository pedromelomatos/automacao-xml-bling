"""Integração do download com a outbox, usando somente XMLs de teste locais."""

from contextlib import ExitStack, contextmanager
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import baixar_xmls
import executar_diario
import integracao_dropbox


DIA = date(2026, 10, 29)
CHAVE = "35261002423640000300550050000037041531922135"
NOTA = {
    "id": 123,
    "numero": "003704",
    "chaveAcesso": CHAVE,
    "situacao": 6,
    "dataEmissao": "2026-10-29 08:19:00",
    "loja": {"id": 205634610},
}
# Fixture mínima para o contrato do parser; não representa uma nota de produção.
XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
  <NFe><infNFe Id="NFe{CHAVE}" versao="4.00">
    <ide><nNF>3704</nNF><serie>5</serie><dhEmi>2026-10-29T08:19:00-03:00</dhEmi></ide>
    <emit><CNPJ>02423640000300</CNPJ></emit>
  </infNFe></NFe>
  <protNFe><infProt><chNFe>{CHAVE}</chNFe><cStat>100</cStat></infProt></protNFe>
</nfeProc>'''.encode("utf-8")


class DownloadEntregasTests(unittest.TestCase):
    @contextmanager
    def pipeline(self, *, configuracao_presente=True, nota=None):
        with tempfile.TemporaryDirectory(prefix="download-outbox-") as temporaria, ExitStack() as stack:
            root = Path(temporaria)
            config = root / "configuracao_dropbox.json"
            if configuracao_presente:
                # O hook usa apenas a existência do opt-in; a configuração de
                # transporte não é carregada, e não há Redis ou rclone no teste.
                config.write_text("{}", encoding="utf-8")
            client = Mock(name="cliente_bling_sem_rede")

            def salvar_xml_fake(cliente, chave, caminho):
                self.assertIs(cliente, client)
                self.assertEqual(chave, CHAVE)
                Path(caminho).write_bytes(XML)

            patches = {
                "DATA_DIR": root,
                "ClienteBling": Mock(return_value=client),
                "obter_access_token": Mock(return_value="token-de-teste-sem-acesso"),
                "consultar_nfes": Mock(return_value=[dict(NOTA if nota is None else nota)]),
                "carregar_configuracao": Mock(return_value={}),
                "configurar_log": Mock(return_value=root / "download-teste.log"),
                "baixar_xml": Mock(side_effect=salvar_xml_fake),
                "organizar_xml": Mock(return_value=SimpleNamespace(
                    novos=0, existentes=1, destinos=(), aviso=None,
                    unidade="Filial", marketplace="Mercado Livre", uf_destino="SP")),
                "criar_zips_gnre": Mock(return_value=()),
                "registrar_metadados_download": Mock(wraps=integracao_dropbox.registrar_metadados_download),
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(baixar_xmls, name, value))
            stack.enter_context(patch.object(baixar_xmls.os, "chdir"))
            stack.enter_context(patch.object(integracao_dropbox, "caminho_configuracao", return_value=config))
            yield SimpleNamespace(root=root, client=client, **{
                k: v for k, v in patches.items() if isinstance(v, Mock)
            })

    def test_download_autorizado_grava_outbox_com_identidade_e_caminho_relativo(self):
        with self.pipeline() as contexto:
            resultado = baixar_xmls.processar_download(DIA, janela_14h=True)
            path = contexto.root / "entregas/metadados" / f"{CHAVE}.json"
            registro = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(resultado.codigo_saida, 0)
            self.assertEqual((resultado.baixados, resultado.existentes, resultado.erros), (1, 0, 0))
            self.assertEqual(registro["chave"], CHAVE)
            self.assertEqual(registro["canal_bling"], "205634610")
            self.assertEqual(registro["situacao_bling"], "6")
            self.assertEqual(registro["cnpj_emitente"], "02423640000300")
            self.assertEqual(registro["arquivo_relativo"], f"xml_originais/29-10-2026/{CHAVE}.xml")
            self.assertEqual(len(registro["sha256"]), 64)
            contexto.client.close.assert_called_once_with()

    def test_falha_de_persistencia_outbox_impede_avanco_e_retry_recupera_xml_cached(self):
        with self.pipeline() as contexto:
            with self.assertLogs(level="ERROR") as logs:
                with patch.object(integracao_dropbox, "salvar_json_atomico", side_effect=OSError("disco sem espaço")):
                    resultado = baixar_xmls.processar_download(DIA, janela_14h=True)
            self.assertIn("falha ao registrar entrega pendente", "\n".join(logs.output))
            self.assertEqual((resultado.baixados, resultado.erros, resultado.codigo_saida), (1, 1, 1))
            contexto.organizar_xml.assert_not_called()
            registrar_janela = Mock()
            with self.assertLogs(level="ERROR"):
                falha = executar_diario.processar_pendencias(
                    [DIA], executar=lambda _: resultado.codigo_saida, registrar=registrar_janela)
            self.assertEqual(falha, DIA)
            registrar_janela.assert_not_called()
            xml_cached = contexto.root / "xml_originais/29-10-2026" / f"{CHAVE}.xml"
            self.assertEqual(xml_cached.read_bytes(), XML)
            contexto.baixar_xml.reset_mock()

            recuperado = baixar_xmls.processar_download(DIA, janela_14h=True)
            self.assertEqual((recuperado.baixados, recuperado.existentes, recuperado.codigo_saida), (0, 1, 0))
            contexto.baixar_xml.assert_not_called()
            self.assertTrue((contexto.root / "entregas/metadados" / f"{CHAVE}.json").is_file())

    def test_xml_cached_sem_metadados_e_registrado_sem_novo_download(self):
        with self.pipeline() as contexto:
            path = contexto.root / "xml_originais/29-10-2026" / f"{CHAVE}.xml"
            path.parent.mkdir(parents=True)
            path.write_bytes(XML)
            resultado = baixar_xmls.processar_download(DIA, janela_14h=True)
            contexto.baixar_xml.assert_not_called()
            contexto.registrar_metadados_download.assert_called_once_with(path, NOTA, data_dir=contexto.root)
            self.assertEqual((resultado.baixados, resultado.existentes, resultado.erros), (0, 1, 0))
            self.assertTrue((contexto.root / "entregas/metadados" / f"{CHAVE}.json").is_file())

    def test_sem_configuracao_dropbox_mantem_download_legado_sem_outbox(self):
        with self.pipeline(configuracao_presente=False) as contexto:
            with patch.object(integracao_dropbox, "campos_xml", side_effect=AssertionError("Não analisar outbox sem opt-in")):
                resultado = baixar_xmls.processar_download(DIA, janela_14h=True)
            self.assertEqual(resultado.codigo_saida, 0)
            self.assertEqual(resultado.baixados, 1)
            self.assertFalse((contexto.root / "entregas").exists())
            contexto.organizar_xml.assert_called_once()

    def test_situacao_5_e_nao_autorizadas_nao_geram_outbox(self):
        for situacao in (5, 2, 9):
            with self.subTest(situacao=situacao), self.pipeline(nota={**NOTA, "situacao": situacao}) as contexto:
                resultado = baixar_xmls.processar_download(DIA, janela_14h=True)
                self.assertEqual(resultado.codigo_saida, 0)
                contexto.registrar_metadados_download.assert_not_called()
                self.assertFalse((contexto.root / "entregas").exists())

    def test_gnre_somente_zip_nao_gera_outbox_para_nota_autorizada(self):
        with self.pipeline() as contexto:
            resultado = baixar_xmls.processar_download(DIA, janela_14h=True, somente_gnre_zip=True)
            self.assertEqual(resultado.codigo_saida, 0)
            self.assertEqual(resultado.baixados, 1)
            contexto.registrar_metadados_download.assert_not_called()
            self.assertFalse((contexto.root / "entregas").exists())
            contexto.criar_zips_gnre.assert_called_once()


if __name__ == "__main__":
    unittest.main()
