"""RPC GNRE restrito, acionado por chave SSH do desktop. Sem shell ou caminhos livres."""

import argparse
import base64
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import zipfile

import acompanhamento_gnre as acompanhamento
import controle_producao_gnre as controle
import emitir_gnre as emitir
from organizar_xmls import primeiro_texto
import operacao_gnre as operacao
from preparar_gnre import ler_nfe, montar_lote, RECEITAS

RAIZ = Path(__file__).resolve().parent
BASE = RAIZ / 'dados-gnre/desktop'
IDENTIDADE = Path.home() / '.local/share/automacao-xml-bling/gnre/identidade.pem'
MAX_PEDIDO = 12 * 1024 * 1024
UF_PILOTO = {'AL', 'SC'}


def agora():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('America/Sao_Paulo'))


def codificar(valor):
    return (json.dumps(valor, ensure_ascii=False, sort_keys=True) + '\n').encode()


def caminho_pedido(identificador):
    if not re.fullmatch(r'[a-f0-9]{32}', identificador or ''):
        raise RuntimeError('Identificador de pedido inválido.')
    return BASE / 'pedidos' / identificador


def carregar(identificador, cliente=None):
    pedido = json.loads((caminho_pedido(identificador) / 'pedido.json').read_text())
    if cliente is not None and pedido['cliente'] != cliente:
        raise RuntimeError('Pedido não pertence a este computador.')
    return pedido


def salvar(pedido):
    emitir.gravar(caminho_pedido(pedido['id']) / 'pedido.json', codificar(pedido))


def trava(nome='pedidos'):
    import fcntl
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    arquivo = (BASE / (nome + '.lock')).open('a+b')
    fcntl.flock(arquivo, fcntl.LOCK_EX)
    return arquivo


def cliente_permitido(cliente):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', cliente):
        raise RuntimeError('Computador inválido.')
    config = json.loads((BASE / 'clientes.json').read_text())
    registro = config.get(cliente)
    if not registro or not registro.get('habilitado'):
        raise RuntimeError('Acesso deste computador desabilitado.')
    return registro


def iniciar_worker():
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (BASE / 'worker.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker'],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def prova_atual(provas, nota):
    prova = next((p for p in provas if p.get('chave') == nota['chave']), None)
    if not prova or str(prova.get('situacao')) not in ('5', '6'):
        raise RuntimeError('Revalide a situação atual desta NF-e no Bling.')
    if not 0 <= time.time() - float(prova.get('consultado_em', 0)) <= 90:
        raise RuntimeError('Consulta da NF-e vencida. Revalide no Bling.')


def dados_nota(pedido, item):
    return ler_nfe(caminho_pedido(pedido['id']) / (item['id'] + '.xml'))


def preparar_item(pedido, item, servico):
    try:
        nota = dados_nota(pedido, item)
        permitidos = cliente_permitido(pedido['cliente'])['cnpjs']
        if nota['cnpj'] not in permitidos:
            raise RuntimeError('Emitente não permitido neste computador.')
        saida = primeiro_texto(nota['xml'], ('ide', 'dhSaiEnt')) or primeiro_texto(nota['xml'], ('ide', 'dSaiEnt'))
        vencimento = pedido.get('vencimento') or (saida[:10] if saida else nota['emissao'].isoformat())
        item.update(chave=nota['chave'], cnpj=nota['cnpj'], uf=nota['uf'],
                    numero=str(int(nota['chave'][25:34])), emissao=nota['emissao'].isoformat(),
                    vencimento=vencimento, pagamento=pedido['pagamento'],
                    impostos={k: str(v) for k, v in nota['valores'].items() if v > 0},
                    elegivel=True)
        item['origens'] = {str(i + 1): '|'.join((nota['cnpj'], nota['uf'], RECEITAS[k], nota['chave']))
                           for i, k in enumerate(item['impostos'])}
        with closing(emitir.abrir_registro(controle.PASTA_PRODUCAO, 'producao')) as con:
            existentes = con.execute('SELECT DISTINCT lote FROM guias WHERE ambiente=? AND identidade IN (' +
                ','.join('?' for _ in item['origens']) + ')', ('producao', *item['origens'].values())).fetchall()
        if existentes:
            if len(existentes) != 1:
                raise RuntimeError('Há registros anteriores em mais de um lote. Confira o controle.')
            item.update(lote_id=existentes[0][0], estado='existente', motivo='Nota já registrada; emissão preservada.')
            return
        configuracoes = {}
        for imposto in item['impostos']:
            receita = RECEITAS[imposto]
            configuracoes[receita] = servico.configuracao(nota['uf'], receita)
        lote = montar_lote(nota, configuracoes, date.fromisoformat(vencimento), date.fromisoformat(pedido['pagamento']))
        emitir.gravar(caminho_pedido(pedido['id']) / (item['id'] + '-lote.xml'), lote)
        item.update(estado='pronta', motivo='Prévia preparada. Aguarda seleção e autorização do piloto.')
        if nota['uf'] not in UF_PILOTO:
            item['estado'] = 'fora_piloto'
            item['motivo'] = 'UF ainda fora do primeiro piloto. Preparação não libera emissão.'
    except Exception as erro:
        item.update(estado='falha_preparo' if item.get('elegivel') else 'entrada_invalida', motivo=str(erro))


def atualizar_resultado(pedido, item, con):
    lote = con.execute('SELECT * FROM lotes WHERE id=?', (item['lote_id'],)).fetchone()
    if lote is None:
        raise RuntimeError('Lote não localizado no controle permanente.')
    item['estado_lote'], item['recibo'] = lote['estado'], lote['recibo']
    pasta = operacao.pasta_lote(controle.PASTA_PRODUCAO, lote)
    resumo = json.loads((pasta / 'resumo.json').read_text()) if (pasta / 'resumo.json').is_file() else {}
    guias = []
    for guia in resumo.get('guias', []):
        classificacao = acompanhamento.classificar_guia(lote['estado'], guia.get('situacao'),
            possui_recibo=bool(lote['recibo']), divergencias=guia.get('conferencia', {}).get('divergencias', []))
        guias.append({**guia, **classificacao})
    if not guias:
        guias = [{**acompanhamento.classificar_guia(lote['estado'], possui_recibo=bool(lote['recibo'])),
                  'receita': RECEITAS[k]} for k in item.get('impostos', {})]
    item['guias'] = guias
    item['resumo'] = acompanhamento.resumir_nota(guias,
        processamento_encerrado=lote['estado'] in ('concluido', 'com_rejeicoes', 'em_revisao', 'rejeitado', 'indeterminado'))
    item['pdf_disponivel'] = bool(resumo.get('liberado_para_pagamento'))
    item['estado'] = item['resumo']['estado']
    item['motivo'] = resumo.get('erro_conferencia') or lote['erro'] or item['resumo']['orientacao']
    if lote['estado'] == 'em_revisao':
        item['motivo'] = '; '.join([d for g in resumo.get('guias', []) for d in g.get('conferencia', {}).get('divergencias', [])]
                                  + resumo.get('conferencia_pdf', {}).get('divergencias', [])) or item['motivo']


def trabalhar():
    import fcntl
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (BASE / 'worker.lock').open('ab') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        limite = time.monotonic() + 600
        servico = emitir.ServicoGnre(IDENTIDADE, 'producao', 25)
        while time.monotonic() < limite:
            encontrou, aguardando = False, False
            for caminho in sorted((BASE / 'pedidos').glob('*/pedido.json')):
                pedido = carregar(caminho.parent.name)
                if pedido['estado'] == 'preparando':
                    encontrou = True
                    for i, item in enumerate(pedido['notas']):
                        if item['estado'] == 'preparando':
                            preparar_item(pedido, item, servico)
                            with trava():
                                atual = carregar(pedido['id'])
                                atual['notas'][i] = item
                                salvar(atual)
                    with trava():
                        atual = carregar(pedido['id'])
                        atual['estado'] = 'preparado'
                        atual['hash_previa'] = hashlib.sha256(codificar(atual['notas'])).hexdigest()
                        salvar(atual)
                elif pedido['estado'] in ('envio_solicitado', 'acompanhando'):
                    encontrou = True
                    for i, item in enumerate(pedido['notas']):
                        if not item.get('selecionada'):
                            continue
                        try:
                            cliente_permitido(pedido['cliente'])
                            with closing(emitir.abrir_registro(controle.PASTA_PRODUCAO, 'producao')) as con:
                                if not item.get('lote_id'):
                                    # Registra antes do POST e persiste o ID antes de tocar a rede.
                                    xml = (caminho.parent / (item['id'] + '-lote.xml')).read_bytes()
                                    lote = emitir.registrar_lote(con, controle.PASTA_PRODUCAO, 'producao', xml,
                                        {k: item['chave'] for k in item['origens']})
                                    item['lote_id'] = lote['id']
                                    with trava():
                                        atual = carregar(pedido['id'])
                                        atual['notas'][i] = item
                                        salvar(atual)
                                    emitir.receber(con, lote, servico)
                                lote = con.execute('SELECT * FROM lotes WHERE id=?', (item['lote_id'],)).fetchone()
                                if lote['estado'] in ('recebido', 'pendente'):
                                    if lote['proxima_consulta'] <= time.time():
                                        emitir.consultar_lote(con, lote, servico)
                                    aguardando = True
                                atualizar_resultado(pedido, item, con)
                        except Exception as erro:
                            # Um envio preparado/indeterminado nunca é repetido pelo worker.
                            item['motivo'] = str(erro)
                            if item.get('lote_id'):
                                with closing(emitir.abrir_registro(controle.PASTA_PRODUCAO, 'producao')) as con:
                                    atualizar_resultado(pedido, item, con)
                            else:
                                item['estado'] = 'falha_antes_envio'
                        with trava():
                            atual = carregar(pedido['id'])
                            atual['notas'][i] = item
                            salvar(atual)
                    with trava():
                        atual = carregar(pedido['id'])
                        atual['estado'] = 'acompanhando' if aguardando else 'finalizado'
                        salvar(atual)
            if not encontrou:
                return
            if aguardando:
                time.sleep(5)


def visao(pedido):
    resposta = json.loads(json.dumps(pedido))
    for item in resposta['notas']:
        item['pode_emitir'] = False
        if item['estado'] == 'pronta':
            try:
                with closing(controle.abrir_controle()) as con:
                    xml = (caminho_pedido(pedido['id']) / (item['id'] + '-lote.xml')).read_bytes()
                    controle.verificar_envio(con, controle.PASTA_PRODUCAO, item['origens'], xml)
                item['pode_emitir'] = True
            except (OSError, RuntimeError) as erro:
                item['bloqueio'] = str(erro)
        if item.get('lote_id'):
            with closing(emitir.abrir_registro(controle.PASTA_PRODUCAO, 'producao')) as con:
                atualizar_resultado(pedido, item, con)
        item['pode_encaminhar'] = bool(item.get('elegivel') and (
            item['estado'] in ('falha_preparo', 'falha_antes_envio', 'fora_piloto') and not item.get('lote_id')
            or item.get('resumo', {}).get('candidato_portal'))) and pedido['estado'] in ('preparado', 'finalizado')
    return resposta


def encaminhar_manual(pedido, payload):
    selecionadas = set(payload.get('notas', []))
    candidatas = [n for n in visao(pedido)['notas'] if n['id'] in selecionadas]
    if not candidatas or len(candidatas) != len(selecionadas) or not all(n['pode_encaminhar'] for n in candidatas):
        raise RuntimeError('Há notas ainda em processamento, parcialmente emitidas ou sem liberação para o portal.')
    for nota in candidatas:
        prova_atual(payload.get('verificacoes_bling', []), nota)
    with trava(), closing(controle.abrir_controle()) as con, con:
        atual = carregar(pedido['id'], pedido['cliente'])
        if atual['estado'] not in ('preparado', 'finalizado'):
            raise RuntimeError('Processamento ativo; aguarde o encerramento.')
        con.execute('BEGIN IMMEDIATE')
        politica = controle.validar_base(controle.PASTA_PRODUCAO, con)
        for nota in candidatas:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='encaminhamentos_manuais'").fetchone():
                anteriores = [con.execute('SELECT pedido FROM encaminhamentos_manuais WHERE identidade=?',
                                           (i,)).fetchone() for i in nota['origens'].values()]
                if all(r and r['pedido'] == pedido['id'] for r in anteriores):
                    continue
            controle._verificar_historico(con, politica, nota['origens'])
            for identidade in nota['origens'].values():
                registros = con.execute('SELECT lotes.* FROM guias JOIN lotes ON lotes.id=guias.lote '
                                        'WHERE guias.ambiente=? AND guias.identidade=?', ('producao', identidade)).fetchall()
                for lote in registros:
                    if lote['estado'] != 'com_rejeicoes':
                        raise RuntimeError('Existe envio anterior que precisa de conferência antes do portal.')
                    resumo = json.loads((operacao.pasta_lote(controle.PASTA_PRODUCAO, lote) / 'resumo.json').read_text())
                    if resumo.get('resultado_sha256') != hashlib.sha256(lote['resultado']).hexdigest() or not resumo.get('guias') or any(
                            g['situacao'] not in ('1', '2') for g in resumo['guias']):
                        raise RuntimeError('Não foi comprovada rejeição de todas as guias anteriores.')
        con.execute('CREATE TABLE IF NOT EXISTS encaminhamentos_manuais('
                    'identidade TEXT PRIMARY KEY, pedido TEXT NOT NULL, encaminhado_em REAL NOT NULL)')
        for nota in candidatas:
            for identidade in nota['origens'].values():
                con.execute('INSERT OR IGNORE INTO encaminhamentos_manuais VALUES(?,?,?)',
                            (identidade, pedido['id'], time.time()))
        # A reserva fica persistida antes de permitir obter os XMLs.
    with trava():
        atual = carregar(pedido['id'], pedido['cliente'])
        for nota in atual['notas']:
            if nota['id'] in selecionadas:
                nota.update(estado='encaminhada_portal', motivo='XML reservado para tratamento manual no GuiasGNRE.')
        atual['hash_previa'] = hashlib.sha256(codificar(atual['notas'])).hexdigest()
        salvar(atual)
    return exportar_manual(atual, selecionadas)


def exportar_manual(pedido, selecionadas):
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, 'w', zipfile.ZIP_DEFLATED) as pacote:
        def adicionar(nome, dados):
            info = zipfile.ZipInfo(nome, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            pacote.writestr(info, dados)
        linhas = ['XMLs reservados para tratamento manual no GuiasGNRE.']
        for nota in pedido['notas']:
            if nota['id'] in selecionadas:
                if nota['estado'] != 'encaminhada_portal':
                    raise RuntimeError('Nota ainda não reservada para encaminhamento.')
                adicionar(nota['chave'] + '.xml',
                    (caminho_pedido(pedido['id']) / (nota['id'] + '.xml')).read_bytes())
                linhas.append(f"CNPJ {nota['cnpj']} / NF-e {nota['numero']} / {nota['uf']}")
        adicionar('LEIA-ME.txt', '\n'.join(linhas) + '\n')
    dados = memoria.getvalue()
    return {'nome': 'XMLs-GNRE-Portal-' + pedido['id'][:8] + '.zip',
            'sha256': hashlib.sha256(dados).hexdigest(), 'conteudo': base64.b64encode(dados).decode()}


def atender(cliente, payload):
    autorizacao = cliente_permitido(cliente)
    acao = payload.get('acao')
    if acao == 'conectar':
        return {'versao': 1, 'cliente': cliente, 'ambiente': 'producao',
                'cnpjs': autorizacao['cnpjs'], 'ufs_piloto': sorted(UF_PILOTO),
                'mensagem': 'Conexão autenticada. Emissão depende de prévia e autorização do lote exato.'}
    if acao == 'preparar':
        ident = payload.get('pedido') or uuid.uuid4().hex
        pasta = caminho_pedido(ident)
        arquivos = payload.get('arquivos', [])
        if not 1 <= len(arquivos) <= 50:
            raise RuntimeError('Selecione entre 1 e 50 XMLs por pedido.')
        pagamento = date.fromisoformat(payload['pagamento'])
        if pagamento < agora().date():
            raise RuntimeError('O pagamento previsto não pode estar no passado.')
        vencimento = payload.get('vencimento')
        if vencimento:
            date.fromisoformat(vencimento)
        digest = hashlib.sha256(codificar(payload)).hexdigest()
        with trava():
            if (pasta / 'pedido.json').exists():
                existente = carregar(ident, cliente)
                if existente['entrada_sha256'] != digest:
                    raise RuntimeError('Este pedido já pertence a outro conteúdo.')
            else:
                pasta.mkdir(mode=0o700, parents=True, exist_ok=True)
                notas, vistos = [], set()
                for arquivo in arquivos:
                    xml = base64.b64decode(arquivo['xml'], validate=True)
                    if not xml or len(xml) > 512 * 1024:
                        raise RuntimeError('XML vazio ou acima do limite de 512 KiB.')
                    doc = hashlib.sha256(xml).hexdigest()
                    if doc in vistos:
                        continue
                    vistos.add(doc)
                    emitir.gravar(pasta / (doc + '.xml'), xml)
                    notas.append({'id': doc, 'nome': str(arquivo['nome']).replace('\\', '/').rsplit('/', 1)[-1][:120],
                                  'estado': 'preparando', 'motivo': 'Consultando regras e conferindo a NF-e.'})
                salvar({'id': ident, 'cliente': cliente, 'entrada_sha256': digest,
                        'criado_em': datetime.now(timezone.utc).isoformat(), 'ambiente': 'producao',
                        'estado': 'preparando', 'pagamento': pagamento.isoformat(),
                        'vencimento': vencimento, 'notas': notas})
        iniciar_worker()
        return visao(carregar(ident, cliente))
    pedido = carregar(payload.get('pedido'), cliente)
    if acao == 'consultar':
        if pedido['estado'] in ('preparando', 'envio_solicitado', 'acompanhando'):
            iniciar_worker()
        return visao(pedido)
    if acao == 'confirmar':
        selecionadas = set(payload.get('notas', []))
        if not selecionadas or not selecionadas.issubset({n['id'] for n in pedido['notas']}):
            raise RuntimeError('Selecione notas da prévia atual.')
        if payload.get('hash_previa') != pedido.get('hash_previa'):
            raise RuntimeError('A prévia mudou. Atualize antes de confirmar.')
        with trava():
            atual = carregar(pedido['id'], cliente)
            if atual['estado'] in ('envio_solicitado', 'acompanhando', 'finalizado'):
                if selecionadas != {n['id'] for n in atual['notas'] if n.get('selecionada')}:
                    raise RuntimeError('Pedido já confirmado com outra seleção.')
            elif atual['estado'] == 'preparado':
                with closing(controle.abrir_controle()) as con:
                    for n in atual['notas']:
                        if n['id'] in selecionadas:
                            if n['estado'] != 'pronta':
                                raise RuntimeError('Nota não está pronta para o piloto.')
                            prova_atual(payload.get('verificacoes_bling', []), n)
                            xml = (caminho_pedido(pedido['id']) / (n['id'] + '-lote.xml')).read_bytes()
                            controle.verificar_envio(con, controle.PASTA_PRODUCAO, n['origens'], xml)
                    for n in atual['notas']:
                        n['selecionada'] = n['id'] in selecionadas
                atual['estado'] = 'envio_solicitado'
                salvar(atual)
            else:
                raise RuntimeError('Aguarde a preparação da prévia.')
        iniciar_worker()
        return visao(carregar(pedido['id'], cliente))
    if acao == 'encaminhar_manual':
        selecionadas = set(payload.get('notas', []))
        if selecionadas and all(n['estado'] == 'encaminhada_portal' for n in pedido['notas'] if n['id'] in selecionadas) and selecionadas.issubset({n['id'] for n in pedido['notas']}):
            return exportar_manual(pedido, selecionadas)
        return encaminhar_manual(pedido, payload)
    if acao == 'pdf':
        item = next((n for n in pedido['notas'] if n['id'] == payload.get('nota')), None)
        if item is None or not item.get('lote_id'):
            raise RuntimeError('Guia não disponível neste pedido.')
        entrega = operacao.entregar(controle.PASTA_PRODUCAO, item['lote_id'], caminho_pedido(pedido['id']) / 'entrega')
        manifesto = json.loads((entrega / 'manifesto.json').read_text())
        pdf = (entrega / manifesto['pdf']).read_bytes()
        return {'nome': f"GNRE-{item['cnpj']}-NF-{item['numero']}-{item['uf']}.pdf",
                'sha256': hashlib.sha256(pdf).hexdigest(), 'conteudo': base64.b64encode(pdf).decode()}
    raise RuntimeError('Operação não permitida.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cliente')
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.worker:
        trabalhar()
        return 0
    try:
        if not args.cliente:
            raise RuntimeError('Computador não identificado.')
        dados = sys.stdin.buffer.read(MAX_PEDIDO + 1)
        if len(dados) > MAX_PEDIDO:
            raise RuntimeError('Pedido acima do limite de tamanho.')
        resposta = {'ok': True, 'resultado': atender(args.cliente, json.loads(dados))}
    except Exception as erro:
        resposta = {'ok': False, 'erro': str(erro)}
    sys.stdout.buffer.write(codificar(resposta))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
