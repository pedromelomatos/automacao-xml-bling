"""Modelo de acompanhamento para o desktop; não transmite nem exporta XMLs.

A classificação de candidato ao portal só vale após uma confirmação de que
o processamento parou. A futura exportação deve reconciliar o histórico e
reservar o encaminhamento antes de disponibilizar o XML ao operador.
"""

from datetime import date


def acompanhar_pagamento(validade, *, hoje, pagamento='nao_conciliado', substituida=False):
    """Validade do documento e confirmação financeira são fatos independentes.

    ``hoje`` deve vir do backend no fuso America/Sao_Paulo. Confirmações de
    pagamento/não pagamento precisam de fonte financeira; uma data passada
    não as comprova. O retorno apenas orienta revisão, sem permitir emissão.
    """
    if type(hoje) is not date:
        raise ValueError('Informe o dia de referência do backend.')
    if pagamento not in ('nao_conciliado', 'pago', 'nao_pago'):
        raise ValueError('Situação financeira desconhecida.')
    limite = date.fromisoformat(validade) if validade else None
    encerrada = limite < hoje if limite else None
    solicitar_atualizacao = False
    if pagamento == 'pago' and substituida:
        estado, titulo = 'pagamento_em_revisao', 'Pagamento de guia substituída: conferir'
        orientacao = 'Confira a guia paga e sua substituta antes de qualquer nova ação.'
    elif pagamento == 'pago':
        estado, titulo = 'pagamento_confirmado', 'Pagamento confirmado'
        orientacao = 'Mantenha o comprovante vinculado à guia.'
    elif substituida:
        estado, titulo = 'substituida', 'Guia substituída'
        orientacao = 'Consulte a guia vinculada como substituta e confira a situação financeira.'
    elif limite is None:
        estado, titulo = 'validade_a_confirmar', 'Validade a confirmar'
        orientacao = 'Confira a validade no retorno antes de liberar o documento.'
    elif encerrada and pagamento == 'nao_pago':
        estado, titulo = 'validade_encerrada_nao_paga', 'Validade encerrada: atualização a avaliar'
        orientacao = 'Revise a regra da UF, a nova data de pagamento e os encargos, preservando a guia anterior.'
        solicitar_atualizacao = True
    elif encerrada:
        estado, titulo = 'validade_encerrada_pagamento_a_confirmar', 'Validade encerrada: conferir pagamento'
        orientacao = 'Confirme com o financeiro se houve pagamento antes de solicitar outra guia.'
    else:
        estado, titulo = 'aguardando_pagamento', 'Aguardando pagamento'
        orientacao = 'Acompanhe a confirmação do financeiro e o prazo do documento.'
    return {'estado': estado, 'titulo': titulo, 'orientacao': orientacao,
            'pagamento': pagamento, 'validade': validade, 'validade_encerrada': encerrada,
            'pode_solicitar_avaliacao_atualizacao': solicitar_atualizacao,
            'reemissao_automatica': False}


def classificar_guia(estado_lote, situacao=None, *, possui_recibo=False, divergencias=()):
    if estado_lote == 'em_revisao' or divergencias:
        return {'estado': 'em_revisao', 'titulo': 'Precisa de conferência',
                'orientacao': 'Confira a divergência antes de qualquer nova emissão.'}
    if estado_lote in ('indeterminado', 'enviando'):
        return {'estado': 'confirmacao_pendente', 'titulo': 'Confirmando o envio',
                'orientacao': 'O envio pode ter sido recebido. Aguarde a confirmação.'}
    if situacao == '0':
        return {'estado': 'gerada', 'titulo': 'Guia gerada',
                'orientacao': 'Aguarde a conferência do PDF para pagamento.'}
    if situacao in ('3', '4') or estado_lote in ('recebido', 'pendente'):
        return {'estado': 'aguardando_sefaz', 'titulo': 'Aguardando a SEFAZ',
                'orientacao': 'O recibo será consultado novamente.'}
    if situacao in ('1', '2'):
        return {'estado': 'rejeitada', 'titulo': 'Guia rejeitada',
                'orientacao': 'Veja o motivo e a orientação da nota para tratamento manual.'}
    if estado_lote == 'rejeitado':
        return {'estado': 'rejeicao_recepcao', 'titulo': 'Envio recusado',
                'orientacao': 'Confira a resposta de recepção antes de usar outro emissor.'}
    if estado_lote in ('preparado', 'nao_enviado') and not possui_recibo:
        return {'estado': 'nao_enviada', 'titulo': 'Ainda não enviada',
                'orientacao': 'A nota continua aguardando uma decisão de emissão.'}
    return {'estado': 'confirmacao_pendente', 'titulo': 'Situação a confirmar',
            'orientacao': 'Confira o registro antes de tentar gerar outra guia.'}


def resumir_nota(guias, *, processamento_encerrado, falha_antes_envio=False,
                 historico_conferido=False, encaminhamento_reservado=False):
    """Uma NF-e pode exigir várias receitas. Nunca encaminhar parcialmente seu XML.

    ``guias`` são classificações do servidor, uma para cada receita esperada.
    Os quatro indicadores são decisões do backend, não campos aceitos do cliente.
    """
    estados = [g['estado'] for g in guias]
    pendentes = {'aguardando_sefaz', 'confirmacao_pendente'}
    if not estados:
        estado, orientacao = 'confirmacao_pendente', 'Confira quais impostos a nota exige.'
        candidato = False
    elif any(e in pendentes for e in estados):
        estado, orientacao = 'aguardando_confirmacao', 'Aguarde a confirmação da SEFAZ antes de enviar a nota ao portal.'
        candidato = False
    elif 'em_revisao' in estados or 'rejeicao_recepcao' in estados:
        estado, orientacao = 'em_revisao', 'Confira o retorno e as possíveis guias antes de outra emissão.'
        candidato = False
    elif all(e == 'gerada' for e in estados):
        estado, orientacao = 'gerada', 'As guias foram geradas; confira o resultado e os PDFs.'
        candidato = False
    elif 'gerada' in estados:
        estado, orientacao = 'parcial', 'Uma parte já foi gerada. Trate somente o imposto faltante; reenviar o XML inteiro pode duplicar a guia existente.'
        candidato = False
    else:
        sem_emissao = all(e == 'rejeitada' for e in estados) or (
            falha_antes_envio and all(e == 'nao_enviada' for e in estados))
        candidato = processamento_encerrado and sem_emissao
        estado = 'falha_sem_emissao' if candidato else 'aguardando_decisao'
        orientacao = ('Nenhuma guia foi gerada. Confira a liberação para tratamento no portal.' if candidato
                      else 'Aguarde o encerramento ou a confirmação de que não houve emissão.')
    liberado = candidato and historico_conferido and encaminhamento_reservado
    if liberado:
        orientacao = 'XML separado para tratamento no GuiasGNRE. A automação não voltará a emitir esta nota.'
    return {'estado': estado, 'orientacao': orientacao, 'guias': guias,
            'candidato_portal': candidato, 'xml_liberado_portal': liberado,
            'pagamento': 'nao_conciliado'}
