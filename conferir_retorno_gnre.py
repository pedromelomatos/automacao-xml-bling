"""Conferência do retorno contra o pedido, sem alterar os dados recebidos da UF."""

from datetime import date
from decimal import Decimal

from preparar_gnre import GNRE, dinheiro, normalizar, texto


G = '{' + GNRE + '}'


def campo(elemento, caminho):
    return elemento.findtext('/'.join(G + parte for parte in caminho.split('/')), default='').strip()


def valores(item):
    resultado = {}
    for valor in item.findall(G + 'valor'):
        tipo = valor.get('tipo')
        if tipo in resultado:
            raise RuntimeError('O retorno contém valor de tipo repetido.')
        resultado[tipo] = dinheiro(valor.text)
    return resultado


def identificacoes(elemento):
    return [(tipo, campo(elemento, 'identificacao/' + tipo)) for tipo in ('CNPJ', 'CPF', 'IE')
            if campo(elemento, 'identificacao/' + tipo)]


def conferir_guia(enviada, retornada):
    divergencias, avisos = [], []
    original = enviada.find(G + 'itensGNRE/' + G + 'item')
    item = retornada.find(G + 'itensGNRE/' + G + 'item')
    antes, depois = valores(original), valores(item)
    zero = Decimal('0.00')
    if '11' not in antes or '11' not in depois or depois['11'] != antes['11']:
        divergencias.append('Principal diferente do envio ou ausente no retorno.')
    if any(v for t, v in depois.items() if t not in ('11', '21', '31', '41', '51')):
        divergencias.append('O retorno contém FCP integrado sem mapeamento neste fluxo.')
    encargos = sum((depois.get(t, zero) for t in ('31', '41', '51')), zero)
    total_calculado = depois.get('11', zero) + encargos
    total = dinheiro(texto(retornada, 'valorGNRE'))
    if total != total_calculado or ('21' in depois and depois['21'] != total):
        divergencias.append('Total da guia diferente da soma do principal e dos encargos.')
    if encargos > 0:
        divergencias.append('A UF acrescentou encargos; conferir antes de liberar para pagamento.')

    for caminho in ('itensGNRE/item/dataVencimento', 'dataPagamento',
                    'itensGNRE/item/referencia/periodo', 'itensGNRE/item/referencia/mes',
                    'itensGNRE/item/referencia/ano', 'itensGNRE/item/referencia/parcela'):
        pedido, retorno = campo(enviada, caminho), campo(retornada, caminho)
        if caminho == 'dataPagamento' and pedido and not retorno:
            # AL e outras UFs omitem a previsão de pagamento e devolvem a validade.
            limite = texto(retornada, 'dataLimitePagamento')
            if limite and date.fromisoformat(limite) >= date.fromisoformat(pedido):
                avisos.append('A UF omitiu dataPagamento; validade cobre o pagamento solicitado.')
            else:
                divergencias.append('Pagamento omitido sem validade que cubra a data solicitada.')
        elif pedido and retorno != pedido:
            divergencias.append(f'{caminho} diferente do envio ou ausente no retorno.')
        elif not pedido and retorno and caminho.endswith('/parcela') and retorno != '0':
            divergencias.append('A UF acrescentou parcela não solicitada.')
    limite = texto(retornada, 'dataLimitePagamento')
    pagamento = campo(enviada, 'dataPagamento')
    if limite and pagamento and date.fromisoformat(limite) < date.fromisoformat(pagamento):
        divergencias.append('A validade retornada termina antes do pagamento solicitado.')

    for grupo in ('contribuinteEmitente', 'itensGNRE/item/contribuinteDestinatario'):
        origem_grupo = enviada.find('/'.join(G + parte for parte in grupo.split('/')))
        retorno_grupo = retornada.find('/'.join(G + parte for parte in grupo.split('/')))
        if origem_grupo is not None and retorno_grupo is not None:
            if identificacoes(origem_grupo) != identificacoes(retorno_grupo):
                divergencias.append(f'{grupo}/identificacao diferente do envio.')
        for nome in ('razaoSocial', 'municipio'):
            caminho = grupo + '/' + nome
            pedido, retorno = campo(enviada, caminho), campo(retornada, caminho)
            if pedido and retorno and normalizar(pedido) != normalizar(retorno):
                divergencias.append(f'{caminho} diferente do envio.')
            elif pedido and not retorno:
                if grupo == 'contribuinteEmitente' and nome == 'razaoSocial':
                    divergencias.append('Razão social do emitente ausente no retorno.')
                else:
                    avisos.append(f'A UF omitiu {caminho}.')

    return {
        'divergencias': divergencias, 'avisos': avisos,
        'principal': str(depois.get('11', zero)), 'multa': str(depois.get('31', zero)),
        'juros': str(depois.get('41', zero)), 'atualizacao': str(depois.get('51', zero)),
        'total': str(total), 'vencimento': campo(retornada, 'itensGNRE/item/dataVencimento'),
        'pagamento_solicitado': pagamento, 'validade': limite,
    }
