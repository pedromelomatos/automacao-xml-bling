from datetime import date
from pathlib import Path
import tempfile
import unittest

from preparar_gnre import ler_nfe, montar_lote
from test_preparar_gnre import NFE, configuracao


class EntradaTests(unittest.TestCase):
    def test_ambientes_tipo_e_finalidade_fora_do_escopo_sao_recusados(self):
        for antes, depois in ((b'<tpAmb>1</tpAmb>', b'<tpAmb>2</tpAmb>'),
                              (b'<tpNF>1</tpNF>', b'<tpNF>0</tpNF>'),
                              (b'<finNFe>1</finNFe>', b'<finNFe>4</finNFe>')):
            with self.subTest(campo=antes), tempfile.TemporaryDirectory() as tmp:
                arquivo = Path(tmp) / 'nota.xml'
                arquivo.write_bytes(NFE.replace(antes, depois))
                with self.assertRaisesRegex(RuntimeError, 'produção|finalidade normal'):
                    ler_nfe(arquivo)

    def test_datas_anteriores_a_emissao_sao_recusadas_sem_impedir_pagamento_antecipado(self):
        with tempfile.TemporaryDirectory() as tmp:
            arquivo = Path(tmp) / 'nota.xml'
            arquivo.write_bytes(NFE)
            nota = ler_nfe(arquivo)
            configs = {r: configuracao(r) for r in ('100102', '100129')}
            with self.assertRaisesRegex(RuntimeError, 'anteriores'):
                montar_lote(nota, configs, date(2026, 10, 28), date(2026, 11, 17))
            with self.assertRaisesRegex(RuntimeError, 'anteriores'):
                montar_lote(nota, configs, date(2026, 11, 17), date(2026, 10, 28))
            self.assertIn(b'<dataPagamento>2026-11-16</dataPagamento>',
                          montar_lote(nota, configs, date(2026, 11, 17), date(2026, 11, 16)))


if __name__ == '__main__':
    unittest.main()
