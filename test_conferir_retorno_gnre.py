from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from lxml import etree

import emitir_gnre as emitir
from preparar_gnre import GNRE
from test_emitir_gnre import RECIBO, ServicoFalso, pdf_exemplo, recepcao, resultado
from test_preparar_gnre import preparar_lote, preparar_lote_sc


G = '{' + GNRE + '}'


class ConferenciaRetornoTests(unittest.TestCase):
    def test_troca_de_tipo_de_documento_do_destinatario_exige_revisao(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
            xml = preparar_lote(pasta)
            lote = emitir.registrar_lote(con, Path(pasta), 'homologacao', xml)
            emitir.receber(con, lote, ServicoFalso([recepcao()]))
            lote = con.execute('SELECT * FROM lotes').fetchone()
            resposta = etree.fromstring(resultado(xml, pdf=pdf_exemplo(xml)))
            cpf = resposta.find('.//' + G + 'contribuinteDestinatario/' + G + 'identificacao/' + G + 'CPF')
            cpf.tag = G + 'CNPJ'
            cpf.text = '00000000000000'
            emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
            self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], 'em_revisao')

    def test_retorno_divergente_retira_pdf_anterior_da_pasta_liberada(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
            lote, resposta = self.preparar(pasta, con)
            emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
            pdf = Path(lote['pasta']) / f'guias-{RECIBO}.pdf'
            self.assertTrue(pdf.exists())
            resposta.find('.//' + G + 'item/' + G + 'valor').text = '999.99'
            emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
            self.assertFalse(pdf.exists())
            self.assertTrue((pdf.parent / 'revisao' / pdf.name).exists())
            self.assertFalse(json.loads((pdf.parent / 'resumo.json').read_text())['liberado_para_pagamento'])

    def preparar(self, pasta, con):
        xml = preparar_lote_sc(pasta)
        lote = emitir.registrar_lote(con, Path(pasta), 'homologacao', xml)
        emitir.receber(con, lote, ServicoFalso([recepcao()]))
        lote = con.execute('SELECT * FROM lotes').fetchone()
        return lote, etree.fromstring(resultado(xml, pdf=pdf_exemplo(xml)))

    def test_divergencia_financeira_data_ou_nome_quarentena_o_pdf(self):
        for caminho, valor in (('itensGNRE/item/valor', '999.99'), ('valorGNRE', '999.99'),
                               ('itensGNRE/item/dataVencimento', '2026-12-31'),
                               ('dataPagamento', '2026-12-31'),
                               ('contribuinteEmitente/razaoSocial', 'OUTRA EMPRESA')):
            with self.subTest(campo=caminho), tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
                lote, resposta = self.preparar(pasta, con)
                guia = resposta.find('.//' + G + 'guia')
                guia.find('/'.join(G + p for p in caminho.split('/'))).text = valor
                emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
                self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], 'em_revisao')
                saida = Path(lote['pasta'])
                self.assertFalse(list(saida.glob('guias-*.pdf')))
                self.assertTrue((saida / 'revisao' / f'guias-{RECIBO}.pdf').is_file())
                resumo = json.loads((saida / 'resumo.json').read_text())
                self.assertFalse(resumo['liberado_para_pagamento'])
                self.assertTrue(resumo['guias'][0]['conferencia']['divergencias'])
                self.assertEqual(con.execute('SELECT count(*) FROM guias').fetchone()[0], 1)

    def test_encargos_corretamente_somados_ficam_discriminados_para_revisao(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
            lote, resposta = self.preparar(pasta, con)
            guia = resposta.find('.//' + G + 'guia')
            item = guia.find(G + 'itensGNRE/' + G + 'item')
            etree.SubElement(item, G + 'valor', tipo='31').text = '0.07'
            etree.SubElement(item, G + 'valor', tipo='21').text = '1.75'
            guia.find(G + 'valorGNRE').text = '1.75'
            emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
            conferencia = json.loads((Path(lote['pasta']) / 'resumo.json').read_text())['guias'][0]['conferencia']
            self.assertEqual((conferencia['principal'], conferencia['multa'], conferencia['total']), ('1.68', '0.07', '1.75'))
            self.assertTrue(any('encargos' in d.lower() for d in conferencia['divergencias']))
            self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], 'em_revisao')

    def test_omissao_de_pagamento_so_aceita_com_validade_suficiente(self):
        for limite, esperado in (('2026-11-13', 'concluido'), ('2026-11-12', 'em_revisao'), ('', 'em_revisao')):
            with self.subTest(limite=limite), tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
                lote, resposta = self.preparar(pasta, con)
                guia = resposta.find('.//' + G + 'guia')
                guia.remove(guia.find(G + 'dataPagamento'))
                if limite:
                    elemento = etree.Element(G + 'dataLimitePagamento')
                    elemento.text = limite
                    guia.insert(list(guia).index(guia.find(G + 'linhaDigitavel')), elemento)
                emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
                self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], esperado)

    def test_valor_repetido_preserva_recibo_e_impede_pdf_liberado(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
            lote, resposta = self.preparar(pasta, con)
            item = resposta.find('.//' + G + 'item')
            etree.SubElement(item, G + 'valor', tipo='11').text = '1.68'
            with self.assertRaisesRegex(RuntimeError, 'repetido'):
                emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
            atual = con.execute('SELECT estado,recibo FROM lotes').fetchone()
            self.assertEqual((atual['estado'], atual['recibo']), ('em_revisao', RECIBO))
            self.assertFalse(list(Path(lote['pasta']).glob('guias-*.pdf')))

    def test_guia_rejeitada_nao_e_liberada_nem_cobrada_por_principal_divergente(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
            lote, resposta = self.preparar(pasta, con)
            guia = resposta.find('.//' + G + 'guia')
            guia.find(G + 'situacaoGuia').text = '2'
            guia.find(G + 'itensGNRE/' + G + 'item/' + G + 'valor').text = '999.99'
            emitir.processar_resultado(con, lote, etree.tostring(resposta), '2')
            self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], 'com_rejeicoes')
            resumo = json.loads((Path(lote['pasta']) / 'resumo.json').read_text())
            self.assertFalse(resumo['liberado_para_pagamento'])


if __name__ == '__main__':
    unittest.main()
