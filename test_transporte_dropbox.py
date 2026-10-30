import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from transporte_dropbox import (
    EntregaIncerta, EnvioDesabilitado, ErroLeitura, ErroTransporte,
    RcloneDropbox, validar_caminho_relativo,
)


CHAVE = "35261002423640000300550050000037041531922135"
DESTINO = f"Filial_Ecommerce/Meli/{CHAVE}-nfe.xml"
XML = (
    f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe>'
    f'<infNFe Id="NFe{CHAVE}"/></NFe><protNFe><infProt>'
    f'<chNFe>{CHAVE}</chNFe><cStat>100</cStat>'
    '</infProt></protNFe></nfeProc>'
).encode()


class TransporteDropboxTests(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta.cleanup)
        self.raiz = Path(self.pasta.name)
        self.exe = self.raiz / "rclone.exe"
        self.exe.touch()
        self.config = self.raiz / "rclone.conf"
        self.config.write_text("[bling_dropbox]\ntype = dropbox\n", encoding="utf-8")
        self.xml = self.raiz / "origem.xml"
        self.xml.write_bytes(XML)

    def transporte(self, **kwargs):
        return RcloneDropbox(self.exe, self.config, **kwargs)

    @patch("transporte_dropbox.subprocess.run")
    def test_simulacao_bloqueia_upload_antes_de_acessar_qualquer_arquivo(self, executar):
        transporte = RcloneDropbox("ausente", self.raiz/"ausente.conf")
        with patch("transporte_dropbox.tempfile.TemporaryDirectory") as criar_temporario:
            with self.assertRaises(EnvioDesabilitado):
                transporte.enviar(self.raiz/"inexistente.xml", "--destino-malicioso")
        executar.assert_not_called()
        criar_temporario.assert_not_called()

    def test_permissao_precisa_ser_booleano_real(self):
        with self.assertRaises(ErroTransporte):
            self.transporte(permitir_envio="false")

    @patch("transporte_dropbox.subprocess.run")
    def test_lista_somente_xmls_e_indexa_chave_em_entrada_e_historico(self, executar):
        historico = DESTINO.replace("Meli/", "Meli/XML Importados/")
        executar.return_value = SimpleNamespace(returncode=0, stdout=f"{DESTINO}\n{historico}\n{DESTINO}\n", stderr="")
        self.assertEqual(self.transporte().listar_xmls(), {CHAVE:[DESTINO,historico]})
        args = executar.call_args.args[0]
        self.assertEqual(args[1:3], ["lsf", "bling_dropbox:"])
        self.assertIn("--recursive", args)
        self.assertFalse(executar.call_args.kwargs["shell"])
        self.assertNotIn("copyto", args)

    @patch("transporte_dropbox.subprocess.run")
    def test_listagem_falha_nao_divulga_saida_bruta_nem_torna_inventario_vazio(self, executar):
        executar.return_value=SimpleNamespace(returncode=1,stdout="segredo stdout",stderr="token=segredo")
        with self.assertRaises(ErroLeitura) as erro:
            self.transporte().listar_xmls()
        self.assertNotIn("segredo", str(erro.exception))
        self.assertNotIn("token", str(erro.exception))

    @patch("transporte_dropbox.subprocess.run")
    def test_timeout_de_listagem_e_sanitizado(self, executar):
        executar.side_effect=subprocess.TimeoutExpired("rclone token=segredo",1,output="segredo")
        with self.assertRaises(ErroLeitura) as erro:
            self.transporte().listar_xmls()
        self.assertNotIn("segredo",str(erro.exception))

    @patch("transporte_dropbox.subprocess.run")
    def test_inventario_invalido_interrompe_leitura(self, executar):
        executar.return_value=SimpleNamespace(returncode=0,stdout=f"{DESTINO}\n../fora.xml\n",stderr="")
        with self.assertRaises(ErroLeitura):
            self.transporte().listar_xmls()

    def test_rejeita_destinos_ambiguos_e_injecao_de_flags(self):
        for caminho in ("../nota.xml","/nota.xml","C:/nota.xml",r"pasta\nota.xml",
                        "Meli//nota.xml","Meli/./nota.xml","Meli/../nota.xml",
                        "--config/nota.xml","Meli/nota.xml\n--delete", "Meli /nota.xml"):
            with self.subTest(caminho=caminho), self.assertRaises(ErroTransporte):
                validar_caminho_relativo(caminho)

    def test_rejeita_remote_com_flags_ou_configuracao_inline(self):
        for remote in ("--config",":dropbox:","bling_dropbox:../pasta","bling_dropbox: --delete","r:\n"):
            with self.subTest(remote=remote),self.assertRaises(ErroTransporte):
                self.transporte(remote=remote)

    @patch("transporte_dropbox.subprocess.run")
    def test_upload_e_unico_explicito_e_usa_snapshot_dos_bytes_validados(self, executar):
        def enviar(args, **kwargs):
            self.assertEqual(args[1],"copyto")
            snapshot=Path(args[2])
            self.assertNotEqual(snapshot,self.xml)
            self.assertEqual(snapshot.read_bytes(),XML)
            self.xml.write_bytes(b"origem modificada durante a transferencia")
            self.assertEqual(snapshot.read_bytes(),XML)
            self.assertEqual(args[3],"bling_dropbox:"+DESTINO)
            self.assertEqual(args[args.index("--dropbox-batch-mode")+1],"off")
            for flag in ("--transfers","--retries","--low-level-retries"):
                self.assertEqual(args[args.index(flag)+1],"1")
            self.assertIn("--immutable",args)
            self.assertFalse(kwargs["shell"])
            self.snapshot=snapshot
            return SimpleNamespace(returncode=0,stdout="",stderr="")
        executar.side_effect=enviar
        self.transporte(permitir_envio=True).enviar(self.xml,DESTINO,sha256_esperado=hashlib.sha256(XML).hexdigest())
        executar.assert_called_once()
        self.assertFalse(self.snapshot.exists())

    @patch("transporte_dropbox.subprocess.run")
    def test_hash_registrado_protege_contra_alteracao_antes_do_snapshot(self, executar):
        digest = hashlib.sha256(XML).hexdigest()
        self.xml.write_bytes(XML + b"<!--conteudo alterado apos planejamento-->")
        for expected in (digest, "hash invalido", True):
            with self.subTest(expected=expected), self.assertRaises(ErroTransporte):
                self.transporte(permitir_envio=True).enviar(self.xml, DESTINO, sha256_esperado=expected)
        executar.assert_not_called()

    @patch("transporte_dropbox.subprocess.run")
    def test_xml_incompleto_ou_chave_divergente_nao_inicia_upload(self, executar):
        for content in (b"<nfeProc>",b"<NFe/>",XML.replace(b"<cStat>100",b"<cStat>101"),
                        XML.replace(CHAVE.encode(),b"1"*44)):
            with self.subTest(content=content):
                self.xml.write_bytes(content)
                with self.assertRaises(ErroTransporte):
                    self.transporte(permitir_envio=True).enviar(self.xml,DESTINO)
        executar.assert_not_called()

    @patch("transporte_dropbox.subprocess.run")
    def test_erro_ou_timeout_do_upload_fica_incerto_sem_retentativa(self, executar):
        for resultado in (SimpleNamespace(returncode=1,stdout="token=segredo",stderr="segredo"),
                          subprocess.TimeoutExpired("rclone",1,output="segredo")):
            executar.reset_mock()
            executar.side_effect=resultado if isinstance(resultado,Exception) else None
            executar.return_value=resultado
            with self.assertRaises(EntregaIncerta) as erro:
                self.transporte(permitir_envio=True).enviar(self.xml,DESTINO)
            self.assertNotIn("segredo",str(erro.exception))
            executar.assert_called_once()

    @patch("transporte_dropbox.subprocess.run")
    def test_configuracao_ausente_falha_antes_de_processos(self, executar):
        self.config.unlink()
        with self.assertRaises(ErroLeitura):
            self.transporte().listar_xmls()
        with self.assertRaises(ErroTransporte):
            self.transporte(permitir_envio=True).enviar(self.xml,DESTINO)
        executar.assert_not_called()


if __name__=="__main__":
    unittest.main()
