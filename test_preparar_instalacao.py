import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import preparar_instalacao as instalacao


class PrimeiraDataTests(unittest.TestCase):
    def test_ultima_janela_ignora_data_do_ambiente(self):
        with patch.dict(os.environ, {"AUTOMACAO_PRIMEIRA_DATA": "2026-09-01"}):
            self.assertEqual(
                instalacao.escolher_primeira_data(date(2026, 10, 8), ultima_janela=True),
                date(2026, 10, 8),
            )

    def test_docker_aceita_data_do_ambiente(self):
        with patch.dict(os.environ, {"AUTOMACAO_PRIMEIRA_DATA": "2026-10-01"}):
            self.assertEqual(instalacao.escolher_primeira_data(date(2026, 10, 8)), date(2026, 10, 1))

    def test_ambiente_rejeita_data_invalida_e_futura(self):
        for valor in ("01/10/2026", "2026-10-09"):
            with self.subTest(valor=valor), patch.dict(os.environ, {"AUTOMACAO_PRIMEIRA_DATA": valor}):
                with self.assertRaises(RuntimeError):
                    instalacao.escolher_primeira_data(date(2026, 10, 8))

    def test_escolha_interativa_repete_apos_data_invalida(self):
        with patch.dict(os.environ, {"AUTOMACAO_PRIMEIRA_DATA": ""}), patch(
            "builtins.input", side_effect=["2", "09/10/2026", "2", "01/10/2026"],
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(
                instalacao.escolher_primeira_data(date(2026, 10, 8), interativo=True),
                date(2026, 10, 1),
            )


class ConexaoTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(instalacao.bling_auth, "CLIENT_ID", "id-teste").start()
        patch.object(instalacao.bling_auth, "CLIENT_SECRET", "segredo-teste").start()
        patch.object(instalacao.bling_auth, "carregar_tokens", return_value={
            "access_token": "teste", "refresh_token": "teste-refresh",
        }).start()
        self.token = patch.object(instalacao.bling_auth, "obter_access_token", return_value="teste").start()
        self.classe = patch.object(instalacao, "ClienteBling").start()
        self.cliente = self.classe.return_value
        self.cliente.get.return_value.json.return_value = {"data": []}

    def test_sem_notas_tambem_confirma_permissao(self):
        instalacao.validar_conexao_bling()
        self.token.assert_called_once_with()
        self.assertEqual(self.cliente.get.call_args.args, ("/nfe",))
        self.assertEqual(self.cliente.get.call_args.kwargs["params"]["limite"], 1)
        self.cliente.close.assert_called_once_with()

    def test_renovacao_com_erro_nao_expoe_resposta(self):
        self.token.side_effect = RuntimeError("HTTP 400: SEGREDO-NA-RESPOSTA")
        with self.assertRaises(RuntimeError) as erro:
            instalacao.validar_conexao_bling()
        self.assertNotIn("SEGREDO-NA-RESPOSTA", str(erro.exception))
        self.classe.assert_not_called()

    def test_recusa_de_permissao_e_falha_de_rede_bloqueiam(self):
        for falha in ("HTTP 401.", "HTTP 403.", "Falha de conexão após 3 tentativas."):
            with self.subTest(falha=falha):
                self.cliente.get.side_effect = RuntimeError(falha)
                with self.assertRaises(RuntimeError):
                    instalacao.validar_conexao_bling()
        self.assertEqual(self.cliente.close.call_count, 3)

    def test_resposta_invalida_nao_confirma_conexao(self):
        for resposta in ({"data": {}}, {"error": "negado"}, []):
            with self.subTest(resposta=resposta):
                self.cliente.get.return_value.json.return_value = resposta
                with self.assertRaises(RuntimeError):
                    instalacao.validar_conexao_bling()

    def test_tokens_incompletos_bloqueiam(self):
        instalacao.bling_auth.carregar_tokens.return_value = {"access_token": "teste"}
        with self.assertRaises(RuntimeError):
            instalacao.validar_conexao_bling()
        self.token.assert_not_called()


class PreparacaoTests(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta.cleanup)
        self.addCleanup(patch.stopall)
        self.raiz = Path(self.pasta.name)
        self.config = self.raiz / "configuracao_execucao.json"
        self.estado = self.raiz / "estado_execucao.json"
        patch.object(instalacao, "DATA_DIR", self.raiz).start()
        patch.object(instalacao, "ARQUIVO_CONFIGURACAO_EXECUCAO", self.config).start()
        patch.object(instalacao, "bloqueio_exclusivo", side_effect=nullcontext).start()
        from executar_diario import carregar_estado, carregar_primeira_data
        patch.object(instalacao, "carregar_estado", side_effect=lambda: carregar_estado(self.estado)).start()
        patch.object(instalacao, "carregar_primeira_data", side_effect=lambda: carregar_primeira_data(self.config)).start()
        self.validacao_local = patch.object(instalacao, "validar_configuracao_local").start()
        self.conexao = patch.object(instalacao, "validar_conexao_bling").start()
        patch.object(instalacao, "datetime", wraps=datetime).start().now.return_value = datetime(2026, 10, 8, 16)
        patch.dict(os.environ, {"AUTOMACAO_PRIMEIRA_DATA": ""}).start()
        patch("sys.stdout", new=io.StringIO()).start()

    def test_salva_escolha_sem_marcar_janela_concluida(self):
        instalacao.preparar_instalacao(primeira_data=date(2026, 10, 1))
        self.assertEqual(json.loads(self.config.read_text())["primeira_data"], "2026-10-01")
        self.assertFalse(self.estado.exists())
        self.conexao.assert_called_once_with()

    def test_falha_na_conexao_nao_salva_configuracao(self):
        self.conexao.side_effect = RuntimeError("Sem acesso")
        with self.assertRaises(RuntimeError):
            instalacao.preparar_instalacao(ultima_janela=True)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.estado.exists())

    def test_validar_somente_nao_pergunta_nem_salva(self):
        with patch("builtins.input") as entrada:
            instalacao.preparar_instalacao(interativo=True, validar_somente=True)
        entrada.assert_not_called()
        self.assertFalse(self.config.exists())
        self.conexao.assert_called_once_with()

    def test_reinstalacao_preserva_escolha(self):
        self.config.write_text('{"primeira_data":"2026-10-01"}', encoding="utf-8")
        original = self.config.read_bytes()
        instalacao.preparar_instalacao(interativo=True)
        self.assertEqual(self.config.read_bytes(), original)

    def test_reinstalacao_preserva_estado_e_ignora_env(self):
        self.estado.write_text('{"ultima_janela_concluida":"2026-10-07"}', encoding="utf-8")
        original = self.estado.read_bytes()
        with patch.dict(os.environ, {"AUTOMACAO_PRIMEIRA_DATA": "2026-09-01"}):
            instalacao.preparar_instalacao(interativo=True)
        self.assertEqual(self.estado.read_bytes(), original)
        self.assertFalse(self.config.exists())

    def test_nao_permite_mudar_data_com_historico(self):
        self.estado.write_text('{"ultima_janela_concluida":"2026-10-07"}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            instalacao.preparar_instalacao(primeira_data=date(2026, 10, 1))
        self.conexao.assert_not_called()


if __name__ == "__main__":
    unittest.main()
