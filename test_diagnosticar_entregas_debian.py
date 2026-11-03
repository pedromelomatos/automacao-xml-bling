import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("diagnostico_entregas", Path(__file__).parent / "scripts/diagnosticar_entregas_debian.py")
diagnostico = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostico)


class DiagnosticoTests(unittest.TestCase):
    def rodar(self, resultado):
        db = SimpleNamespace(ping=Mock(), listar=Mock(return_value=[]),
                             obter_proximo_lote_em=Mock(return_value=0),
                             _cliente=SimpleNamespace(ttl=Mock(return_value=-2)), _lock_key="teste:lock")
        transporte = SimpleNamespace(_validar_instalacao=Mock(), remote="teste:",
                                     _executar=Mock(return_value=resultado))
        cfg = dict(habilitada=True, modo="envio", max_por_lote=5,
                   intervalo_lotes_segundos=60, tempo_limite_segundos=120)
        saida = io.StringIO()
        with patch.object(diagnostico.i, "carregar_configuracao", return_value=cfg), \
             patch.object(diagnostico.i, "criar_registro", return_value=db), \
             patch.object(diagnostico.i, "carregar_metadados", return_value=[]), \
             patch.object(diagnostico.i, "carregar_historico", return_value=[]), \
             patch.object(diagnostico.i, "criar_transporte", return_value=transporte) as criar_transporte, \
             patch.object(diagnostico.i, "planejar", return_value={"resumo": {}, "lotes": []}), \
             patch("sys.stdout", saida):
            codigo = diagnostico.diagnosticar()
        criar_transporte.assert_called_once_with(cfg)
        self.assertEqual(transporte._executar.call_args.args[0][0], "lsf")
        return codigo, saida.getvalue()

    def test_falha_de_rede_classificada_sem_expor_stderr(self):
        codigo, saida = self.rodar(SimpleNamespace(returncode=1, stdout="", stderr="token=NAO-EXIBIR dial tcp: i/o timeout"))
        self.assertEqual(codigo, 1)
        self.assertIn("rede_ou_timeout", saida)
        self.assertNotIn("NAO-EXIBIR", saida)

    def test_inventario_com_nome_invalido_identifica_causa(self):
        codigo, saida = self.rodar(SimpleNamespace(returncode=0, stdout="Filial_Ecommerce/Meli/XML Importados/nota (1).xml\n", stderr=""))
        self.assertEqual(codigo, 1)
        self.assertIn("caminho_invalido_no_inventario", saida)

    def test_leitura_e_planejamento_sem_metodos_de_mutacao(self):
        codigo, saida = self.rodar(SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.assertEqual(codigo, 0)
        self.assertIn('"uploads_executados": 0', saida)
        self.assertIn('"alteracoes_redis": 0', saida)


if __name__ == "__main__":
    unittest.main()
