from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lxml import etree

import emitir_gnre as emitir
import operacao_gnre as operacao
from conferir_pagamento_gnre import conferir_codigo, conferir_pdf, digito
from preparar_gnre import GNRE
from test_emitir_gnre import RECIBO, ServicoFalso, codigo_exemplo, pdf_exemplo, recepcao, resultado
from test_preparar_gnre import preparar_lote_sc


G = '{' + GNRE + '}'


class OperacaoTests(unittest.TestCase):
    def test_http_429_encerra_rodada_sem_reenvio_e_respeita_retry_after(self):
        with tempfile.TemporaryDirectory() as base, closing(emitir.abrir_registro(Path(base))) as con:
            self.preparar(base, con)
            con.execute('UPDATE lotes SET proxima_consulta=0')
            con.commit()
            erro = emitir.consulta.ErroHttpGnre('HTTP 429', 429, 900)
            servico = ServicoFalso([erro])
            with patch.object(operacao.time, 'time', return_value=1000):
                self.assertEqual(len(operacao.retomar(base, servico, 'homologacao')), 1)
            self.assertEqual(con.execute('SELECT proxima_consulta FROM lotes').fetchone()[0], 1900)
            self.assertEqual(servico.chamadas, ['resultado'])

    def preparar(self, base, con, producao=False):
        xml = preparar_lote_sc(base)
        lote = emitir.registrar_lote(con, Path(base), 'homologacao', xml)
        emitir.receber(con, lote, ServicoFalso([recepcao()]))
        if producao:
            con.execute("UPDATE lotes SET ambiente='producao'")
            con.commit()
        lote = con.execute('SELECT * FROM lotes').fetchone()
        resposta = resultado(xml, pdf=pdf_exemplo(xml, not producao))
        if producao:
            resposta = resposta.replace(b'<ambiente>2</ambiente>', b'<ambiente>1</ambiente>')
        return xml, lote, resposta

    def test_codigo_real_da_imagem_sc_e_ambos_modulos(self):
        linha = '858000000003016800980006000002611201423932722002'
        codigo = ''.join(linha[i:i + 11] for i in range(0, 48, 12))
        self.assertEqual(conferir_codigo(codigo, linha, '1.68')['total'], '1.68')
        self.assertEqual(digito('01230067896', 10), '3')
        self.assertEqual(digito('01230067896', 11), '0')
        for modulo, indicador in ((10, '6'), (11, '8')):
            base = '85' + indicador + '00000000168' + '0' * 29
            codigo = base[:3] + digito(base, modulo) + base[3:]
            linha = ''.join(codigo[i:i + 11] + digito(codigo[i:i + 11], modulo) for i in range(0, 44, 11))
            self.assertEqual(conferir_codigo(codigo, linha, '1.68')['modulo'], modulo)
            for ruim, outra in ((codigo, linha[:-1] + str((int(linha[-1]) + 1) % 10)), ('1' * 44, linha)):
                with self.assertRaises(ValueError):
                    conferir_codigo(ruim, outra, '1.68')
            with self.assertRaisesRegex(ValueError, 'Valor'):
                conferir_codigo(codigo, linha, '999.99')

    def test_pdf_vazio_ou_de_outra_guia_e_codigo_ausente_retidos(self):
        for defeito in ('vazio', 'cnpj', 'codigo', 'linha', 'homologacao'):
            with self.subTest(defeito=defeito), tempfile.TemporaryDirectory() as base, closing(emitir.abrir_registro(Path(base))) as con:
                xml, lote, resposta = self.preparar(base, con, producao=defeito == 'homologacao')
                raiz = etree.fromstring(resposta)
                if defeito == 'codigo':
                    campo = raiz.find('.//' + G + 'codigoBarras')
                    campo.getparent().remove(campo)
                elif defeito == 'linha':
                    linha = raiz.find('.//' + G + 'linhaDigitavel')
                    linha.text = linha.text[:-1] + str((int(linha.text[-1]) + 1) % 10)
                else:
                    import base64
                    pdf = pdf_exemplo(xml)
                    if defeito == 'vazio':
                        pdf = pdf.replace(b' Tj', b'   ')
                    elif defeito == 'cnpj':
                        cnpj = emitir.identificar_lote(xml)[0].encode()
                        pdf = pdf.replace(cnpj, b'9' * len(cnpj))
                    raiz.find('.//' + G + 'pdfGuias').text = base64.b64encode(pdf).decode()
                emitir.processar_resultado(con, lote, etree.tostring(raiz), '1' if defeito == 'homologacao' else '2')
                atual = con.execute('SELECT * FROM lotes').fetchone()
                self.assertEqual((atual['estado'], atual['recibo']), ('em_revisao', RECIBO))
                self.assertFalse(list(Path(lote['pasta']).glob('guias-*.pdf')))
                self.assertFalse(json.loads((Path(lote['pasta']) / 'resumo.json').read_text())['liberado_para_pagamento'])

    def test_entrega_confere_pdf_e_e_idempotente_expiracao_e_homologacao_bloqueadas(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, destino = Path(tmp) / 'base', Path(tmp) / 'entrega'
            with closing(emitir.abrir_registro(base)) as con:
                xml, lote, resposta = self.preparar(base, con, True)
                emitir.processar_resultado(con, lote, resposta, '1')
            saida = operacao.entregar(base, lote['id'], destino, date(2026, 11, 12))
            self.assertEqual(operacao.entregar(base, lote['id'], destino, date(2026, 11, 12)), saida)
            manifesto = json.loads((saida / 'manifesto.json').read_text())
            self.assertEqual(manifesto['pagamento'], 'nao_conciliado')
            self.assertEqual((saida / manifesto['pdf']).read_bytes(), pdf_exemplo(xml, False))
            with self.assertRaisesRegex(RuntimeError, 'Validade'):
                operacao.entregar(base, lote['id'], destino, date(2027, 1, 1))
            original = Path(lote['pasta']) / manifesto['pdf']
            original.write_bytes(original.read_bytes() + b'alterado')
            with self.assertRaisesRegex(RuntimeError, 'alterado'):
                operacao.entregar(base, lote['id'], destino, date(2026, 11, 12))
        with tempfile.TemporaryDirectory() as tmp, closing(emitir.abrir_registro(Path(tmp))) as con:
            _, lote, resposta = self.preparar(tmp, con)
            emitir.processar_resultado(con, lote, resposta, '2')
            with self.assertRaisesRegex(RuntimeError, 'produção'):
                operacao.entregar(tmp, lote['id'], Path(tmp) / 'entrega')

    def test_retomada_so_consulta_mesmo_recibo_respeita_intervalo_e_lease(self):
        with tempfile.TemporaryDirectory() as base, closing(emitir.abrir_registro(Path(base))) as con:
            xml, lote, resposta = self.preparar(base, con)
            servico = ServicoFalso([TimeoutError('queda'), resposta])
            con.execute('UPDATE lotes SET proxima_consulta=0,consultando_ate=9999999999')
            con.commit()
            self.assertEqual(operacao.retomar(base, servico, 'homologacao'), [])
            con.execute('UPDATE lotes SET consultando_ate=0')
            con.commit()
            self.assertEqual(operacao.retomar(base, servico, 'homologacao')[0]['estado'], 'recebido')
            self.assertEqual(operacao.retomar(base, servico, 'homologacao'), [])
            con.execute('UPDATE lotes SET proxima_consulta=0')
            con.commit()
            self.assertEqual(operacao.retomar(base, servico, 'homologacao')[0]['estado'], 'concluido')
            self.assertEqual(servico.chamadas, ['resultado', 'resultado'])
            self.assertEqual(con.execute('SELECT recibo FROM lotes').fetchone()[0], RECIBO)

    def test_backup_restaura_reservas_recibos_e_pdf_sem_segredos_e_sem_sobrescrever(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, backup, restaurado = Path(tmp) / 'base', Path(tmp) / 'backup.zip', Path(tmp) / 'isolado'
            with closing(emitir.abrir_registro(base)) as con:
                _, lote, resposta = self.preparar(base, con)
                emitir.processar_resultado(con, lote, resposta, '2')
                con.executescript("CREATE TABLE politica_producao(id INTEGER, pasta TEXT, fluxo_exclusivo INTEGER);"
                                  "INSERT INTO politica_producao VALUES(1,'ativo',1);"
                                  "CREATE TABLE historico_externo(identidade TEXT);"
                                  "INSERT INTO historico_externo VALUES('reserva-importada');")
                con.commit()
            (base / 'identidade.pem').write_text('segredo-fora-do-backup')
            hash_antes = hashlib.sha256((base / 'controle.sqlite3').read_bytes()).hexdigest()
            manifesto = operacao.criar_backup(base, backup)
            with zipfile.ZipFile(backup) as z:
                self.assertNotIn('identidade.pem', z.namelist())
                self.assertFalse(any(b'segredo-fora-do-backup' in z.read(n) for n in z.namelist()))
            operacao.restaurar_backup(backup, restaurado)
            with closing(operacao.abrir_existente(restaurado)) as con:
                atual = con.execute('SELECT * FROM lotes').fetchone()
                self.assertEqual((atual['recibo'], atual['estado']), (RECIBO, 'concluido'))
                self.assertTrue(Path(atual['pasta']).is_relative_to(restaurado))
                self.assertEqual(con.execute('SELECT fluxo_exclusivo FROM politica_producao').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT identidade FROM historico_externo').fetchone()[0], 'reserva-importada')
                self.assertEqual(con.execute('SELECT COUNT(*) FROM guias').fetchone()[0], 1)
                self.assertEqual((Path(atual['pasta']) / f'guias-{RECIBO}.pdf').read_bytes(), pdf_exemplo(atual['xml']))
            self.assertEqual(hash_antes, hashlib.sha256((base / 'controle.sqlite3').read_bytes()).hexdigest())
            with self.assertRaisesRegex(RuntimeError, 'pasta nova'):
                operacao.restaurar_backup(backup, base)
            adulterado = Path(tmp) / 'adulterado.zip'
            with zipfile.ZipFile(backup) as fonte, zipfile.ZipFile(adulterado, 'w') as z:
                for n in fonte.namelist():
                    z.writestr(n, b'incorreto' if n == 'controle.sqlite3' else fonte.read(n))
            with self.assertRaisesRegex(RuntimeError, 'Hash'):
                operacao.restaurar_backup(adulterado, Path(tmp) / 'invalido')
            self.assertFalse((Path(tmp) / 'invalido').exists())


if __name__ == '__main__':
    unittest.main()
