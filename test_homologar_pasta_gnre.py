from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import homologar_pasta_gnre as atalho


class TestAtalhoHomologacao(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporario.cleanup)
        self.raiz = Path(self.temporario.name)
        self.chave = "1" * 44
        self.nota = {"chave": self.chave, "uf": "SC"}
        self.dados = self.raiz / "dados-gnre" / "atual"

    def registrar(self, pasta, estado, recibo=None):
        pasta.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(pasta / "controle.sqlite3") as conexao:
            conexao.execute("CREATE TABLE lotes(id, estado, recibo, pasta, origens, ambiente)")
            conexao.execute("INSERT INTO lotes VALUES(?,?,?,?,?,?)", (
                "lote-antigo", estado, recibo, str(pasta / "resultado"),
                json.dumps({"guia": "cnpj|SC|100102|" + self.chave}), "homologacao",
            ))

    def processar(self, simular=False):
        return atalho.processar_xml(
            Path("nota.xml"), self.raiz, Path("cliente.py"), self.dados,
            Path("identidade.pem"), "2026-11-16", simular, lambda _: self.nota,
        )

    @patch.object(atalho, "executar_cliente")
    def test_nota_aceita_em_outro_ensaio_nao_e_reenviada(self, executar):
        antigo = self.raiz / "dados-gnre" / "ensaios" / "antigo"
        self.registrar(antigo, "concluido", "recibo-antigo")
        resultado = self.processar()
        executar.assert_not_called()
        self.assertEqual(resultado["codigo"], 0)
        self.assertEqual(resultado["lotes"][0]["dados"], str(antigo))

    @patch.object(atalho, "executar_cliente", return_value=2)
    def test_pendente_consulta_o_registro_original(self, executar):
        antigo = self.raiz / "dados-gnre" / "ensaios" / "antigo"
        self.registrar(antigo, "pendente", "recibo-antigo")
        resultado = self.processar()
        argumentos = executar.call_args.args
        self.assertEqual(argumentos[1], str(antigo))
        self.assertEqual(argumentos[3], ["consultar", "--lote-id", "lote-antigo", "--aguardar", "120"])
        self.assertEqual(resultado["codigo"], 2)

    @patch.object(atalho, "executar_cliente")
    def test_envio_incerto_sem_recibo_nao_e_repetido(self, executar):
        self.registrar(self.dados, "indeterminado")
        self.assertEqual(self.processar()["codigo"], 1)
        executar.assert_not_called()

    @patch.object(atalho, "executar_cliente")
    def test_simulacao_nova_nao_acessa_servico(self, executar):
        resultado = self.processar(simular=True)
        executar.assert_not_called()
        self.assertEqual(resultado["acao"], "novo_envio")
        self.assertFalse(self.dados.exists())

    @patch.object(atalho, "executar_cliente")
    def test_banco_invalido_impede_novo_envio(self, executar):
        self.dados.mkdir(parents=True)
        (self.dados / "controle.sqlite3").write_bytes(b"nao e sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            self.processar()
        executar.assert_not_called()

    @patch.object(atalho, "executar_cliente", return_value=0)
    def test_nova_nota_so_usa_homologacao_e_datas_do_teste(self, executar):
        self.processar()
        self.assertEqual(executar.call_args.args[1], self.dados)
        self.assertEqual(executar.call_args.args[3], [
            "executar", "--xml", "nota.xml", "--vencimento", "2026-11-16",
            "--pagamento", "2026-11-16", "--aguardar", "120",
        ])


if __name__ == "__main__":
    unittest.main()
