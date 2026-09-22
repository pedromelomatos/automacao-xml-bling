import tempfile
import unittest
from pathlib import Path

from organizar_xmls import organizar_xml


CONFIGURACAO = {
    "unidades": {
        "11111111111111": "Matriz",
        "22222222222222": "Filial",
    },
    "canais": {"1": "Marketplace Teste"},
    "intermediadores": {},
    "gnre_ufs_por_unidade": {
        "Matriz": [
            "AC", "AL", "AP", "AM", "CE", "MA", "MS", "PA",
            "PB", "PI", "RN", "RO", "RR", "SE", "TO", "DF",
        ],
        "Filial": [
            "AC", "AL", "AP", "AM", "BA", "CE", "GO", "MA",
            "MT", "MS", "PA", "PB", "PR", "PE", "PI", "RN",
            "RO", "RR", "SC", "SE", "TO", "DF",
        ],
    },
}


def criar_xml(caminho, cnpj_emitente, uf_destino):
    chave = "3" * 44
    caminho.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">
  <NFe>
    <infNFe Id="NFe{chave}">
      <ide><dhEmi>2026-09-21T10:00:00-03:00</dhEmi></ide>
      <emit><CNPJ>{cnpj_emitente}</CNPJ></emit>
      <dest><enderDest><UF>{uf_destino}</UF></enderDest></dest>
    </infNFe>
  </NFe>
  <protNFe><infProt><chNFe>{chave}</chNFe></infProt></protNFe>
</nfeProc>
""",
        encoding="utf-8",
    )


class OrganizacaoGnreTests(unittest.TestCase):
    def organizar(self, cnpj_emitente, uf_destino):
        temporario = tempfile.TemporaryDirectory()
        self.addCleanup(temporario.cleanup)
        raiz = Path(temporario.name)
        origem = raiz / "nota.xml"
        saida = raiz / "organizados"
        criar_xml(origem, cnpj_emitente, uf_destino)
        resultado = organizar_xml(
            origem,
            nota={"loja": {"id": 1}},
            pasta_saida=saida,
            configuracao=CONFIGURACAO,
        )
        return resultado

    def test_matriz_cria_copia_gnre_para_uf_configurada(self):
        resultado = self.organizar("11111111111111", "CE")

        self.assertEqual(resultado.novos, 2)
        self.assertEqual(len(resultado.destinos), 2)
        self.assertEqual(
            resultado.destinos[1].parts[-6:],
            (
                "Matriz",
                "Marketplace Teste",
                "GNRE",
                "21-09-2026",
                "CE",
                f"{'3' * 44}.xml",
            ),
        )
        self.assertTrue(all(destino.exists() for destino in resultado.destinos))

    def test_matriz_nao_cria_copia_gnre_para_bahia(self):
        resultado = self.organizar("11111111111111", "BA")

        self.assertEqual(resultado.novos, 1)
        self.assertEqual(len(resultado.destinos), 1)
        self.assertNotIn("GNRE", resultado.destinos[0].parts)

    def test_filial_cria_copia_gnre_para_bahia(self):
        resultado = self.organizar("22222222222222", "BA")

        self.assertEqual(resultado.novos, 2)
        self.assertEqual(len(resultado.destinos), 2)
        self.assertEqual(
            resultado.destinos[1].parts[-6:-3],
            ("Filial", "Marketplace Teste", "GNRE"),
        )


if __name__ == "__main__":
    unittest.main()
