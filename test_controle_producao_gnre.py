from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import controle_producao_gnre as controle
import emitir_gnre as emitir
from test_emitir_gnre import ServicoFalso, soap
from test_preparar_gnre import CHAVE, preparar_lote_sc

CNPJ = CHAVE[6:20]
NS = 'http://guiasgnre/webgnre'


class ControleProducaoTests(unittest.TestCase):
    def test_indice_reduzido_preserva_bloqueio_sem_dados_do_destinatario(self):
        self.ativar()
        self.historico_com(True)
        indice = self.raiz / 'indice.json'
        digest = controle.exportar_indice(self.historico, indice, [CNPJ])
        documento = json.loads(indice.read_text())
        self.assertEqual(set(documento['guias'][0]), {'identidade', 'controle', 'operacao', 'consultado_em', 'fonte_sha256'})
        self.assertEqual(controle.importar_historico(indice, digest), 1)
        with closing(emitir.abrir_registro(self.pasta, 'producao')) as con:
            with self.assertRaisesRegex(RuntimeError, 'GuiasGNRE'):
                emitir.registrar_lote(con, self.pasta, 'producao', self.xml)

    def test_indice_alterado_ou_sem_consulta_obrigatoria_e_recusado(self):
        self.historico_com(True)
        indice = self.raiz / 'indice.json'
        digest = controle.exportar_indice(self.historico, indice, [CNPJ])
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            controle.importar_historico(indice)
        documento = json.loads(indice.read_text())
        documento['consultas'].pop()
        conteudo = json.dumps(documento).encode()
        indice.write_bytes(conteudo)
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            controle.importar_historico(indice, digest)
        with self.assertRaisesRegex(RuntimeError, 'Faltam consultas'):
            controle.importar_historico(indice, hashlib.sha256(conteudo).hexdigest())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        self.pasta = self.raiz / 'fixo'
        self.patch = patch.object(controle, 'PASTA_PRODUCAO', self.pasta)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        controle.inicializar([CNPJ])
        self.xml = preparar_lote_sc(self.raiz)
        self.historico = self.raiz / 'historico'
        self.historico.mkdir()

    def ativar(self):
        with closing(controle.abrir_controle()) as con, con:
            con.execute('UPDATE politica_producao SET fluxo_exclusivo=1')

    def historico_com(self, existente=False, pendente=False):
        consultas = []
        for op in controle.OPERACOES:
            raiz = ET.Element('{http://www.w3.org/2003/05/soap-envelope}Envelope')
            corpo = ET.SubElement(raiz, '{http://www.w3.org/2003/05/soap-envelope}Body')
            resposta = ET.SubElement(corpo, '{' + NS + '}' + op + 'Response')
            resultado = ET.SubElement(resposta, '{' + NS + '}' + op + 'Result')
            guias = ET.SubElement(resultado, '{' + NS + '}guias')
            tem = existente and op == controle.OPERACOES[int(pendente)]
            if tem:
                guia = ET.SubElement(guias, '{' + NS + '}tGuias')
                for nome, valor in (('emitente', CNPJ), ('chave_nfe', CHAVE), ('uf_destinatario', 'SC'),
                                    ('receita', '100102'), ('num_controle', '1234567890123456')):
                    ET.SubElement(guia, '{' + NS + '}' + nome).text = valor
            ET.SubElement(resultado, '{' + NS + '}retorno').text = '0' if tem else '3'
            conteudo = ET.tostring(raiz)
            nome = CNPJ + '-' + op + '.xml'
            (self.historico / nome).write_bytes(conteudo)
            consultas.append({'cnpj': CNPJ, 'operacao': op, 'concluida': True, 'codigo': '0' if tem else '3',
                              'guias': int(tem), 'arquivo': nome, 'sha256': hashlib.sha256(conteudo).hexdigest(),
                              'consultado_em': datetime.now(timezone.utc).isoformat()})
        (self.historico / 'consultas.json').write_text(json.dumps(consultas))
        return consultas

    def preparar_envio(self, con):
        self.ativar()
        self.historico_com()
        controle.importar_historico(self.historico)
        return emitir.registrar_lote(con, self.pasta, 'producao', self.xml)

    def test_controle_novo_bloqueado_e_nao_recriado(self):
        with self.assertRaises(FileExistsError):
            controle.inicializar([CNPJ])
        with closing(emitir.abrir_registro(self.pasta, 'producao')) as con:
            with self.assertRaisesRegex(RuntimeError, 'fluxo exclusivo'):
                emitir.registrar_lote(con, self.pasta, 'producao', self.xml)
            self.assertEqual(con.execute('SELECT count(*) FROM lotes').fetchone()[0], 0)

    def test_outra_pasta_ou_conexao_nao_contorna_bloqueio(self):
        outra = self.raiz / 'outra'
        with closing(emitir.abrir_registro(outra)) as con:
            with self.assertRaisesRegex(RuntimeError, 'controle fixo'):
                emitir.registrar_lote(con, outra, 'producao', self.xml)
            with self.assertRaisesRegex(RuntimeError, 'conexão'):
                emitir.registrar_lote(con, self.pasta, 'producao', self.xml)
        with self.assertRaisesRegex(RuntimeError, 'controle fixo'):
            emitir.abrir_registro(self.raiz / 'nova', 'producao')
        self.assertFalse((self.raiz / 'nova').exists())

    def test_autorizada_e_pendente_externas_bloqueiam_emissao(self):
        self.ativar()
        for pendente in (False, True):
            with self.subTest(pendente=pendente):
                self.historico_com(True, pendente)
                controle.importar_historico(self.historico)
                with closing(emitir.abrir_registro(self.pasta, 'producao')) as con:
                    with self.assertRaisesRegex(RuntimeError, 'GuiasGNRE'):
                        emitir.registrar_lote(con, self.pasta, 'producao', self.xml)
                    self.assertEqual(con.execute('SELECT count(*) FROM lotes').fetchone()[0], 0)

    def test_nota_nova_exige_consulta_e_nao_duplica_mudando_datas(self):
        self.ativar()
        with closing(emitir.abrir_registro(self.pasta, 'producao')) as con:
            with self.assertRaisesRegex(RuntimeError, 'histórico'):
                emitir.registrar_lote(con, self.pasta, 'producao', self.xml)
            self.historico_com()
            controle.importar_historico(self.historico)
            lote = emitir.registrar_lote(con, self.pasta, 'producao', self.xml)
            self.assertEqual(lote['estado'], 'preparado')
            with self.assertRaisesRegex(RuntimeError, 'Já existe envio'):
                emitir.registrar_lote(con, self.pasta, 'producao', self.xml.replace(b'2026-11-13', b'2026-11-14'))

    def test_historico_expirado_ou_futuro_impede_envio(self):
        self.ativar()
        self.historico_com()
        controle.importar_historico(self.historico)
        with closing(emitir.abrir_registro(self.pasta, 'producao')) as con:
            for delta in (-601, 120):
                with con:
                    con.execute('UPDATE sincronizacoes SET consultado_em=?', (controle.time.time() + delta,))
                with self.assertRaisesRegex(RuntimeError, 'vencida'):
                    emitir.registrar_lote(con, self.pasta, 'producao', self.xml)

    def test_rechecagem_antes_do_post_detecta_guia_importada_depois(self):
        with closing(emitir.abrir_registro(self.pasta, 'producao')) as con:
            lote = self.preparar_envio(con)
            self.historico_com(True)
            controle.importar_historico(self.historico)
            servico = ServicoFalso([])
            with self.assertRaisesRegex(RuntimeError, 'GuiasGNRE'):
                emitir.receber(con, lote, servico)
            self.assertEqual(servico.chamadas, [])
            self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], 'preparado')

    def test_snapshot_parcial_alterado_falho_ou_inconclusivo_nao_atualiza(self):
        for caso in ('parcial', 'alterado', 'falha', 'inconclusivo'):
            with self.subTest(caso=caso):
                consultas = self.historico_com(True)
                if caso == 'parcial':
                    consultas.pop()
                elif caso == 'falha':
                    consultas[0]['concluida'] = False
                elif caso == 'inconclusivo':
                    nome = self.historico / consultas[0]['arquivo']
                    conteudo = nome.read_bytes().replace(b'>0<', b'>2<')
                    nome.write_bytes(conteudo)
                    consultas[0]['sha256'] = hashlib.sha256(conteudo).hexdigest()
                else:
                    (self.historico / consultas[0]['arquivo']).write_bytes(b'<outro/>')
                (self.historico / 'consultas.json').write_text(json.dumps(consultas))
                with self.assertRaises(RuntimeError):
                    controle.importar_historico(self.historico)
                with closing(controle.abrir_controle()) as con:
                    self.assertEqual(con.execute('SELECT count(*) FROM historico_externo').fetchone()[0], 0)
                    self.assertEqual(con.execute('SELECT count(*) FROM sincronizacoes').fetchone()[0], 0)

    def test_importacao_idempotente_preserva_reserva_que_sumiu_da_api(self):
        self.historico_com(True)
        controle.importar_historico(self.historico)
        controle.importar_historico(self.historico)
        self.historico_com(False)
        controle.importar_historico(self.historico)
        with closing(controle.abrir_controle()) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM historico_externo').fetchone()[0], 1)

    def test_rejeicao_na_recepcao_preserva_reserva_em_producao(self):
        with closing(emitir.abrir_registro(self.pasta, 'producao')) as con:
            lote = self.preparar_envio(con)
            resposta = soap(b'<TRetLote_GNRE xmlns="http://www.gnre.pe.gov.br"><ambiente>1</ambiente>'
                           b'<situacaoRecepcao><codigo>197</codigo><descricao>Erro</descricao></situacaoRecepcao></TRetLote_GNRE>')
            with self.assertRaisesRegex(RuntimeError, 'rejeitado'):
                emitir.processar_recepcao(con, lote, resposta, '1')
            self.assertEqual(con.execute('SELECT count(*) FROM guias').fetchone()[0], 1)

    def test_envios_concorrentes_reservam_uma_unica_transmissao(self):
        with closing(emitir.abrir_registro(self.pasta, 'producao')) as primeiro, closing(
                emitir.abrir_registro(self.pasta, 'producao')) as segundo:
            lote = self.preparar_envio(primeiro)
            servico = ServicoFalso([])
            servico.ambiente = '1'
            def envio(tipo, xml):
                atual = segundo.execute('SELECT * FROM lotes').fetchone()
                with self.assertRaisesRegex(RuntimeError, 'tentativa'):
                    emitir.receber(segundo, atual, ServicoFalso([]))
                return soap(b'<TRetLote_GNRE xmlns="http://www.gnre.pe.gov.br"><ambiente>1</ambiente>'
                            b'<situacaoRecepcao><codigo>100</codigo><descricao>OK</descricao></situacaoRecepcao>'
                            b'<recibo><numero>20260000000001</numero></recibo></TRetLote_GNRE>')
            with patch.object(servico, 'chamar', side_effect=envio) as chamada:
                emitir.receber(primeiro, lote, servico)
            self.assertEqual(chamada.call_count, 1)


if __name__ == '__main__':
    unittest.main()
