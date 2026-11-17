import base64
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import controle_producao_gnre as controle
import desktop_gnre_servidor as servidor
import emitir_gnre as emitir
from test_emitir_gnre import ServicoFalso
from test_preparar_gnre import CHAVE, NFE_SC, configuracao_sc


class DesktopServidorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        for objeto, campo, valor in ((servidor, 'BASE', self.raiz / 'desktop'),
                                      (controle, 'PASTA_PRODUCAO', self.raiz / 'producao')):
            p = patch.object(objeto, campo, valor)
            p.start()
            self.addCleanup(p.stop)
        self.worker = patch.object(servidor, 'iniciar_worker').start()
        self.addCleanup(patch.stopall)
        servidor.BASE.mkdir()
        (servidor.BASE / 'clientes.json').write_text(json.dumps({'teste': {'habilitado': True, 'cnpjs': [CHAVE[6:20]]},
                                                               'outro': {'habilitado': True, 'cnpjs': [CHAVE[6:20]]}}))
        controle.inicializar([CHAVE[6:20]])
        with closing(controle.abrir_controle()) as con, con:
            for op in controle.OPERACOES:
                con.execute('INSERT INTO sincronizacoes VALUES(?,?,?,?)', (CHAVE[6:20], op, time.time(), 'f' * 64))

    def preparar(self):
        payload = {'acao': 'preparar', 'pedido': uuid.uuid4().hex, 'pagamento': servidor.agora().date().isoformat(),
                   'arquivos': [{'nome': 'nota.xml', 'xml': base64.b64encode(NFE_SC).decode()}]}
        servidor.atender('teste', payload)
        pedido = servidor.carregar(payload['pedido'])
        falso = type('Servico', (), {'configuracao': lambda s, u, r: configuracao_sc()})()
        servidor.preparar_item(pedido, pedido['notas'][0], falso)
        pedido['estado'] = 'preparado'
        pedido['hash_previa'] = hashlib.sha256(servidor.codificar(pedido['notas'])).hexdigest()
        servidor.salvar(pedido)
        return pedido, payload

    def autorizar(self, pedido, minutos=120):
        nota = pedido['notas'][0]
        xml = (servidor.caminho_pedido(pedido['id']) / (nota['id'] + '-lote.xml')).read_bytes()
        return controle.autorizar_piloto(xml, minutos=minutos)

    def confirmar(self, pedido):
        return servidor.atender('teste', {'acao': 'confirmar', 'pedido': pedido['id'],
            'hash_previa': pedido['hash_previa'], 'notas': [pedido['notas'][0]['id']],
            'verificacoes_bling': [{'chave': CHAVE, 'situacao': '5', 'consultado_em': time.time()}]})

    def test_previa_nao_emite_e_reenvio_da_mesma_preparacao_e_idempotente(self):
        pedido, payload = self.preparar()
        self.assertEqual(pedido['notas'][0]['estado'], 'pronta')
        self.assertFalse(servidor.visao(pedido)['notas'][0]['pode_emitir'])
        self.assertEqual(servidor.atender('teste', payload)['id'], pedido['id'])
        with closing(controle.abrir_controle()) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM lotes').fetchone()[0], 0)
        payload['pagamento'] = '2030-01-01'
        with self.assertRaisesRegex(RuntimeError, 'outro conteúdo'):
            servidor.atender('teste', payload)

    def test_autorizacao_exata_expira_sem_ativar_transicao(self):
        pedido, _ = self.preparar()
        digest = self.autorizar(pedido)
        self.assertTrue(servidor.visao(pedido)['notas'][0]['pode_emitir'])
        with closing(controle.abrir_controle()) as con, con:
            self.assertEqual(con.execute('SELECT fluxo_exclusivo FROM politica_producao').fetchone()[0], 0)
            con.execute('UPDATE pilotos_producao SET expira_em=? WHERE sha256=?', (time.time() - 1, digest))
        self.assertFalse(servidor.visao(pedido)['notas'][0]['pode_emitir'])
        self.autorizar(pedido)
        arquivo = servidor.caminho_pedido(pedido['id']) / (pedido['notas'][0]['id'] + '-lote.xml')
        arquivo.write_bytes(arquivo.read_bytes().replace(b'>1.68<', b'>999.99<'))
        self.assertFalse(servidor.visao(pedido)['notas'][0]['pode_emitir'])

    def test_confirmacao_exige_previa_inalterada_e_bling_recente(self):
        pedido, _ = self.preparar()
        self.autorizar(pedido)
        with self.assertRaisesRegex(RuntimeError, 'prévia mudou'):
            servidor.atender('teste', {'acao': 'confirmar', 'pedido': pedido['id'], 'hash_previa': '0' * 64,
                                     'notas': [pedido['notas'][0]['id']]})
        for situacao, momento in (('2', time.time()), ('5', time.time() - 120)):
            with self.assertRaises(RuntimeError):
                servidor.prova_atual([{'chave': CHAVE, 'situacao': situacao, 'consultado_em': momento}], pedido['notas'][0])

    def test_timeout_no_post_e_confirmacao_repetida_nao_reenviam(self):
        pedido, _ = self.preparar()
        self.autorizar(pedido)
        self.confirmar(pedido)
        self.confirmar(pedido)
        servico = ServicoFalso([TimeoutError('Sem resposta de recepção')])
        servico.ambiente = '1'
        with patch.object(emitir, 'ServicoGnre', return_value=servico):
            servidor.trabalhar()
            servidor.trabalhar()
        self.assertEqual(servico.chamadas, ['recepcao'])
        with closing(controle.abrir_controle()) as con:
            self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], 'indeterminado')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM guias').fetchone()[0], 1)

    def test_pedido_de_outro_cliente_e_caminho_livre_sao_bloqueados(self):
        pedido, _ = self.preparar()
        with self.assertRaisesRegex(RuntimeError, 'outro computador|não pertence'):
            servidor.atender('outro', {'acao': 'consultar', 'pedido': pedido['id']})
        with self.assertRaisesRegex(RuntimeError, 'Identificador'):
            servidor.atender('teste', {'acao': 'consultar', 'pedido': '../../segredos'})

    def test_manual_reserva_antes_de_exportar_e_bloqueia_emissao_futura(self):
        pedido, _ = self.preparar()
        nota = pedido['notas'][0]
        nota.update(estado='falha_preparo', motivo='UF sem mapeamento')
        servidor.salvar(pedido)
        payload = {'acao': 'encaminhar_manual', 'pedido': pedido['id'], 'notas': [nota['id']],
                   'verificacoes_bling': [{'chave': CHAVE, 'situacao': '5', 'consultado_em': time.time()}]}
        resposta = servidor.atender('teste', payload)
        self.assertTrue(resposta['nome'].endswith('.zip'))
        self.assertEqual(servidor.atender('teste', payload)['sha256'], resposta['sha256'])
        # Recupera interrupção entre a reserva no banco e a atualização do pedido.
        interrompido = servidor.carregar(pedido['id'])
        interrompido['notas'][0]['estado'] = 'falha_preparo'
        servidor.salvar(interrompido)
        self.assertEqual(servidor.atender('teste', payload)['sha256'], resposta['sha256'])
        with closing(controle.abrir_controle()) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM encaminhamentos_manuais').fetchone()[0], 1)
        with self.assertRaisesRegex(RuntimeError, 'tratamento manual'):
            self.autorizar(pedido)

    def test_manual_nao_exporta_nota_pronta_ou_parcial(self):
        pedido, _ = self.preparar()
        payload = {'acao': 'encaminhar_manual', 'pedido': pedido['id'], 'notas': [pedido['notas'][0]['id']]}
        with self.assertRaisesRegex(RuntimeError, 'sem liberação'):
            servidor.atender('teste', payload)
        pedido['notas'][0]['estado'] = 'parcial'
        pedido['notas'][0]['resumo'] = {'candidato_portal': False}
        servidor.salvar(pedido)
        with self.assertRaisesRegex(RuntimeError, 'sem liberação'):
            servidor.atender('teste', payload)


if __name__ == '__main__':
    unittest.main()
