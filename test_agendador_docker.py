import unittest
from datetime import datetime

from agendador_docker import executar_com_retentativas, proxima_execucao


class AgendadorDockerTests(unittest.TestCase):
    def test_proxima_execucao_no_mesmo_dia_antes_das_14h10(self):
        self.assertEqual(
            proxima_execucao(datetime(2026, 9, 21, 9, 0)),
            datetime(2026, 9, 21, 14, 10),
        )

    def test_proxima_execucao_no_dia_seguinte_apos_as_14h10(self):
        self.assertEqual(
            proxima_execucao(datetime(2026, 9, 21, 14, 10)),
            datetime(2026, 9, 22, 14, 10),
        )

    def test_repete_ate_obter_sucesso(self):
        codigos = iter([1, 1, 0])
        esperas = []

        sucesso = executar_com_retentativas(
            executar=lambda: next(codigos),
            esperar=esperas.append,
        )

        self.assertTrue(sucesso)
        self.assertEqual(esperas, [600, 600])

    def test_para_apos_tres_falhas(self):
        tentativas = []

        sucesso = executar_com_retentativas(
            executar=lambda: tentativas.append(1) or 1,
            esperar=lambda _: None,
        )

        self.assertFalse(sucesso)
        self.assertEqual(len(tentativas), 3)


if __name__ == "__main__":
    unittest.main()
