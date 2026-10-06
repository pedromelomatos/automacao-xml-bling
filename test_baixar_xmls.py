import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path

from baixar_xmls import calcular_periodo, criar_zips_gnre, filtrar_emitidas_na_janela


class PeriodoConsultaTests(unittest.TestCase):
    def test_hoje_ate_agora_vai_de_ontem_as_14h_ate_a_consulta(self):
        for hora in (0, 13, 14, 16, 23):
            with self.subTest(hora=hora):
                agora = datetime(2026, 10, 6, hora, 12, 34, 500)
                self.assertEqual(
                    calcular_periodo(agora.date(), True, ate_agora=True, agora=agora),
                    (datetime(2026, 10, 5, 14), agora.replace(microsecond=0)),
                )

    def test_janela_completa_mantem_corte_das_14h(self):
        self.assertEqual(
            calcular_periodo(date(2026, 10, 6), True, agora=datetime(2026, 10, 6, 16)),
            (datetime(2026, 10, 5, 14), datetime(2026, 10, 6, 14)),
        )
        with self.assertRaisesRegex(RuntimeError, "ainda não terminou"):
            calcular_periodo(date(2026, 10, 6), True, agora=datetime(2026, 10, 6, 13))

    def test_hoje_ate_agora_rejeita_outra_data(self):
        for dia in (5, 7):
            with self.subTest(dia=dia), self.assertRaisesRegex(RuntimeError, "data de hoje"):
                calcular_periodo(date(2026, 10, dia), True, ate_agora=True, agora=datetime(2026, 10, 6, 13))

    def test_hoje_ate_agora_inclui_notas_apos_as_14h_e_exclui_fora_do_periodo(self):
        inicio, fim = calcular_periodo(
            date(2026, 10, 6), True, ate_agora=True, agora=datetime(2026, 10, 6, 16),
        )
        emissoes = ("2026-10-05T13:59:59", "2026-10-05T14:00:00",
                    "2026-10-06T12:59:59", "2026-10-06T15:59:59", "2026-10-06T16:00:00")
        notas = [{"dataEmissao": emissao, "situacao": 6} for emissao in emissoes]
        selecionadas, fora, invalidas = filtrar_emitidas_na_janela(notas, inicio, fim)
        self.assertEqual([nota["dataEmissao"] for nota in selecionadas], list(emissoes[1:4]))
        self.assertEqual((fora, invalidas), (2, 0))


class ZipGnreTests(unittest.TestCase):
    def test_cria_um_zip_por_unidade_com_nome_e_estrutura(self):
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            xml_matriz = raiz / "1.xml"
            xml_filial = raiz / "2.xml"
            xml_matriz.write_text("<xml>matriz</xml>", encoding="utf-8")
            xml_filial.write_text("<xml>filial</xml>", encoding="utf-8")

            zips = criar_zips_gnre(
                [
                    ("Matriz", "Mercado Livre", "CE", xml_matriz),
                    ("Filial", "Shopee", "BA", xml_filial),
                ],
                date(2026, 9, 25),
                pasta_saida=raiz / "zips",
            )

            self.assertEqual(
                {caminho.name for caminho in zips},
                {"Matriz 25-09-2026.zip", "Filial 25-09-2026.zip"},
            )
            with zipfile.ZipFile(raiz / "zips" / "Matriz 25-09-2026.zip") as arquivo:
                self.assertEqual(arquivo.namelist(), ["Mercado Livre/CE/1.xml"])
            with zipfile.ZipFile(raiz / "zips" / "Filial 25-09-2026.zip") as arquivo:
                self.assertEqual(arquivo.namelist(), ["Shopee/BA/2.xml"])

    def test_sem_gnre_nao_cria_zip(self):
        with tempfile.TemporaryDirectory() as pasta:
            self.assertEqual(
                criar_zips_gnre([], date(2026, 9, 25), pasta_saida=pasta),
                (),
            )


if __name__ == "__main__":
    unittest.main()
