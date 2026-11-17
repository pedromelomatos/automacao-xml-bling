import unittest
from datetime import date

from acompanhamento_gnre import acompanhar_pagamento, classificar_guia, resumir_nota


class AcompanhamentoTests(unittest.TestCase):
    def test_validade_cobre_o_dia_e_termina_no_seguinte_sem_inferir_nao_pagamento(self):
        dentro = acompanhar_pagamento('2026-11-17', hoje=date(2026, 11, 17))
        depois = acompanhar_pagamento('2026-11-17', hoje=date(2026, 11, 18))
        self.assertEqual(dentro['estado'], 'aguardando_pagamento')
        self.assertFalse(dentro['validade_encerrada'])
        self.assertEqual(depois['estado'], 'validade_encerrada_pagamento_a_confirmar')
        self.assertEqual(depois['pagamento'], 'nao_conciliado')
        self.assertFalse(depois['pode_solicitar_avaliacao_atualizacao'])

    def test_pagamento_confirmado_impede_atualizacao_mesmo_apos_validade(self):
        resumo = acompanhar_pagamento('2026-11-17', hoje=date(2026, 11, 18), pagamento='pago')
        self.assertEqual(resumo['estado'], 'pagamento_confirmado')
        self.assertFalse(resumo['pode_solicitar_avaliacao_atualizacao'])

    def test_nao_pago_confirmado_permite_so_avaliar_atualizacao(self):
        resumo = acompanhar_pagamento('2026-11-17', hoje=date(2026, 11, 18), pagamento='nao_pago')
        self.assertEqual(resumo['estado'], 'validade_encerrada_nao_paga')
        self.assertTrue(resumo['pode_solicitar_avaliacao_atualizacao'])
        self.assertFalse(resumo['reemissao_automatica'])
        antes = acompanhar_pagamento('2026-11-17', hoje=date(2026, 11, 17), pagamento='nao_pago')
        self.assertFalse(antes['pode_solicitar_avaliacao_atualizacao'])

    def test_sem_validade_ou_substituida_nao_libera_atualizacao(self):
        for validade, substituida, pagamento, estado in (
            (None, False, 'nao_pago', 'validade_a_confirmar'),
            ('2026-11-17', True, 'nao_pago', 'substituida'),
            ('2026-11-17', True, 'pago', 'pagamento_em_revisao'),
        ):
            with self.subTest(estado=estado):
                resumo = acompanhar_pagamento(validade, hoje=date(2026, 11, 18),
                                              pagamento=pagamento, substituida=substituida)
                self.assertEqual(resumo['estado'], estado)
                self.assertFalse(resumo['pode_solicitar_avaliacao_atualizacao'])

    def test_confirmacao_financeira_desconhecida_e_data_invalida_sao_recusadas(self):
        with self.assertRaisesRegex(ValueError, 'Situação financeira'):
            acompanhar_pagamento('2026-11-17', hoje=date(2026, 11, 18), pagamento='acho_que_nao')
        with self.assertRaises(ValueError):
            acompanhar_pagamento('2026-02-30', hoje=date(2026, 11, 18))

    def resumo(self, *guias, **opcoes):
        return resumir_nota(guias, processamento_encerrado=True, historico_conferido=True,
                            encaminhamento_reservado=True, **opcoes)

    def test_timeout_e_recibo_pendente_nao_liberam_portal(self):
        for estado, situacao, recibo in (('indeterminado', None, False), ('enviando', None, False),
                                        ('recebido', None, True), ('pendente', '4', True),
                                        ('desconhecido', None, False)):
            with self.subTest(estado=estado):
                guia = classificar_guia(estado, situacao, possui_recibo=recibo)
                self.assertFalse(self.resumo(guia)['xml_liberado_portal'])

    def test_difal_gerado_fcp_rejeitado_nao_libera_xml_inteiro(self):
        difal = classificar_guia('com_rejeicoes', '0')
        fcp = classificar_guia('com_rejeicoes', '2')
        resumo = self.resumo(difal, fcp)
        self.assertEqual(resumo['estado'], 'parcial')
        self.assertFalse(resumo['xml_liberado_portal'])
        self.assertIn('somente o imposto faltante', resumo['orientacao'])

    def test_guia_aceita_em_lote_pendente_e_reconhecida_sem_liberar_a_nota(self):
        resumo = self.resumo(classificar_guia('pendente', '0'), classificar_guia('pendente', '4'))
        self.assertEqual(resumo['guias'][0]['estado'], 'gerada')
        self.assertEqual(resumo['estado'], 'aguardando_confirmacao')
        self.assertFalse(resumo['xml_liberado_portal'])

    def test_revisao_e_rejeicao_de_recepcao_precisam_confirmacao(self):
        for guia in (classificar_guia('em_revisao', '0'), classificar_guia('rejeitado'),
                     classificar_guia('concluido', '0', divergencias=['valor diferente'])):
            self.assertFalse(self.resumo(guia)['xml_liberado_portal'])

    def test_so_rejeicoes_confirmadas_com_historico_e_reserva_liberam(self):
        guias = [classificar_guia('com_rejeicoes', '2')]
        for encerrado, historico, reservado in ((False, True, True), (True, False, True), (True, True, False)):
            resumo = resumir_nota(guias, processamento_encerrado=encerrado,
                                 historico_conferido=historico, encaminhamento_reservado=reservado)
            self.assertFalse(resumo['xml_liberado_portal'])
        self.assertTrue(self.resumo(*guias)['xml_liberado_portal'])

    def test_nao_enviado_exige_falha_confirmada_e_encerramento(self):
        guia = classificar_guia('preparado')
        self.assertFalse(self.resumo(guia)['xml_liberado_portal'])
        self.assertTrue(self.resumo(guia, falha_antes_envio=True)['xml_liberado_portal'])

    def test_guias_geradas_nao_significam_pagamento(self):
        resumo = self.resumo(classificar_guia('concluido', '0'))
        self.assertEqual(resumo['estado'], 'gerada')
        self.assertEqual(resumo['pagamento'], 'nao_conciliado')
        self.assertFalse(resumo['xml_liberado_portal'])

    def test_lista_vazia_nao_libera_nota(self):
        self.assertFalse(self.resumo()['xml_liberado_portal'])


if __name__ == '__main__':
    unittest.main()
