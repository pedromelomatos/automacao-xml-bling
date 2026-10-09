import json
import tempfile
import unittest
from contextlib import nullcontext
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import executar_diario

from executar_diario import (
    calcular_pendencias,
    carregar_primeira_data,
    carregar_estado,
    processar_pendencias,
    salvar_estado,
    ultima_janela_encerrada,
)


class ExecutorDiarioTests(unittest.TestCase):
    def test_primeira_data_configurada_e_validada(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "configuracao_execucao.json"
            self.assertIsNone(carregar_primeira_data(caminho))
            caminho.write_text('{"primeira_data":"2026-10-01"}', encoding="utf-8")
            self.assertEqual(carregar_primeira_data(caminho), date(2026, 10, 1))
            caminho.write_text('{"primeira_data":"invalida"}', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                carregar_primeira_data(caminho)

    def test_executor_usa_escolha_ate_haver_historico(self):
        with patch.object(executar_diario, "obter_argumentos", return_value=SimpleNamespace(
            primeira_data=None, simular=False,
        )), patch.object(executar_diario, "configurar_log"), patch.object(
            executar_diario, "bloqueio_exclusivo", side_effect=nullcontext,
        ), patch.object(executar_diario, "carregar_estado", return_value=None) as estado, patch.object(
            executar_diario, "carregar_primeira_data", return_value=date(2026, 10, 6),
        ) as inicial, patch.object(
            executar_diario, "ultima_janela_encerrada", return_value=date(2026, 10, 8),
        ), patch.object(executar_diario, "processar_pendencias", return_value=None) as processar:
            self.assertEqual(executar_diario.main(), 0)
            processar.assert_called_once_with([date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)])
            estado.return_value = date(2026, 10, 7)
            inicial.reset_mock()
            processar.reset_mock()
            self.assertEqual(executar_diario.main(), 0)
            processar.assert_called_once_with([date(2026, 10, 8)])
            inicial.assert_not_called()

    def test_janela_atual_so_fica_disponivel_a_partir_das_14h(self):
        self.assertEqual(
            ultima_janela_encerrada(datetime(2026, 9, 21, 13, 59, 59)),
            date(2026, 9, 20),
        )
        self.assertEqual(
            ultima_janela_encerrada(datetime(2026, 9, 21, 14, 0, 0)),
            date(2026, 9, 21),
        )

    def test_primeira_execucao_processa_apenas_a_ultima_janela(self):
        self.assertEqual(
            calcular_pendencias(None, date(2026, 9, 20)),
            [date(2026, 9, 20)],
        )

    def test_recupera_todos_os_dias_apos_o_ultimo_sucesso(self):
        self.assertEqual(
            calcular_pendencias(date(2026, 9, 17), date(2026, 9, 20)),
            [date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)],
        )

    def test_estado_e_gravado_e_lido(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "estado.json"
            salvar_estado(
                date(2026, 9, 20),
                caminho,
                agora=datetime(2026, 9, 21, 14, 10),
            )
            self.assertEqual(carregar_estado(caminho), date(2026, 9, 20))
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            self.assertEqual(dados["atualizado_em"], "2026-09-21T14:10:00")

    def test_falha_interrompe_sem_registrar_a_data_com_erro(self):
        datas = [date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)]
        executadas = []
        registradas = []

        def executar(data_execucao):
            executadas.append(data_execucao)
            return 1 if data_execucao == date(2026, 9, 19) else 0

        falha = processar_pendencias(datas, executar, registradas.append)

        self.assertEqual(falha, date(2026, 9, 19))
        self.assertEqual(executadas, datas[:2])
        self.assertEqual(registradas, [date(2026, 9, 18)])


if __name__ == "__main__":
    unittest.main()
