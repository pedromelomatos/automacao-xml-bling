import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from baixar_xmls import criar_zips_gnre


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
