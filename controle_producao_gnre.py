"""Base fixa e histórico externo para impedir duplicidade de GNRE em produção."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from consultar_gnre import UFS, carregar_xml


PASTA_PRODUCAO = (Path('/dados/gnre') if Path('/run/gnre').is_dir()
                  else Path.home() / 'gnre-emissao/dados-gnre') / 'producao-operacao'
OPERACOES = ('BuscaTodasGuiasAutorizadas', 'BuscaTodasGuiasPendentes')
VALIDADE_HISTORICO = 600  # Uma cópia antiga nunca serve como consulta negativa atual.


def inicializar(cnpjs):
    cnpjs = sorted(set(cnpjs))
    if not cnpjs or any(not re.fullmatch(r'[A-Z0-9]{14}', c) for c in cnpjs):
        raise RuntimeError('Informe os CNPJs permitidos na operação de produção.')
    PASTA_PRODUCAO.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PASTA_PRODUCAO.is_symlink():
        raise RuntimeError('A base de produção não pode ser um link.')
    banco = PASTA_PRODUCAO / 'controle.sqlite3'
    # Não recriar ou substituir um controle que já exista.
    fd = os.open(banco, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(banco)) as con, con:
        con.executescript('''
            CREATE TABLE politica_producao(id INTEGER PRIMARY KEY CHECK(id=1), pasta TEXT NOT NULL,
                cnpjs TEXT NOT NULL, fluxo_exclusivo INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE historico_externo(identidade TEXT NOT NULL, controle TEXT NOT NULL,
                operacao TEXT NOT NULL, consultado_em REAL NOT NULL, fonte_sha256 TEXT NOT NULL,
                PRIMARY KEY(identidade, controle, operacao));
            CREATE TABLE sincronizacoes(cnpj TEXT NOT NULL, operacao TEXT NOT NULL,
                consultado_em REAL NOT NULL, fonte_sha256 TEXT NOT NULL, PRIMARY KEY(cnpj,operacao));
        ''')
        con.execute('INSERT INTO politica_producao(id,pasta,cnpjs) VALUES(1,?,?)',
                    (str(PASTA_PRODUCAO.resolve()), json.dumps(cnpjs)))
    # Mantém o esquema reconhecível também pelo leitor de históricos de homologação.
    from emitir_gnre import abrir_registro
    with closing(abrir_registro(PASTA_PRODUCAO)):
        pass
    return banco


def validar_base(pasta, conexao=None):
    pasta = Path(pasta)
    banco = pasta / 'controle.sqlite3'
    if pasta.resolve() != PASTA_PRODUCAO.resolve() or pasta.is_symlink() or banco.is_symlink() or not banco.is_file():
        raise RuntimeError('Produção exige o controle fixo já inicializado; outra pasta ou base nova não é permitida.')
    if conexao is not None:
        arquivo = conexao.execute('PRAGMA database_list').fetchone()[2]
        if Path(arquivo).resolve() != banco.resolve():
            raise RuntimeError('A conexão não pertence ao controle fixo de produção.')
        try:
            politica = conexao.execute('SELECT * FROM politica_producao WHERE id=1').fetchone()
        except sqlite3.Error as e:
            raise RuntimeError('Controle de produção sem política inicializada.') from e
        if politica is None or politica['pasta'] != str(PASTA_PRODUCAO.resolve()):
            raise RuntimeError('A política não corresponde ao caminho do controle de produção.')
        return politica
    return banco


def abrir_controle():
    banco = validar_base(PASTA_PRODUCAO)
    con = sqlite3.connect(banco.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        validar_base(PASTA_PRODUCAO, con)
    except Exception:
        con.close()
        raise
    return con


def texto(elemento, nome):
    for filho in elemento:
        if filho.tag.rsplit('}', 1)[-1] == nome:
            return (filho.text or '').strip()
    return ''


def ler_historico(pasta, cnpjs):
    pasta = Path(pasta)
    consultas = json.loads((pasta / 'consultas.json').read_text(encoding='utf-8-sig'))
    esperadas = {(c, op) for c in cnpjs for op in OPERACOES}
    vistas, sincronizacoes, guias = set(), [], []
    for consulta in consultas:
        par = (consulta['cnpj'], consulta['operacao'])
        if par not in esperadas or par in vistas or consulta.get('concluida') is not True:
            raise RuntimeError('Histórico incompleto, repetido ou com consulta externa não concluída.')
        vistas.add(par)
        nome = par[0] + '-' + par[1] + '.xml'
        if consulta.get('arquivo') != nome:
            raise RuntimeError('Nome de arquivo de histórico incompatível com a consulta.')
        conteudo = (pasta / nome).read_bytes()
        digest = hashlib.sha256(conteudo).hexdigest()
        if digest != consulta.get('sha256'):
            raise RuntimeError('O histórico foi alterado depois da consulta.')
        raiz = carregar_xml(conteudo)
        resultados = [e for e in raiz.iter() if e.tag.rsplit('}', 1)[-1] == par[1] + 'Result']
        if len(resultados) != 1:
            raise RuntimeError('Histórico sem resultado único da operação de consulta.')
        resultado = resultados[0]
        codigo = texto(resultado, 'retorno')
        registros = [e for e in resultado.iter() if e.tag.rsplit('}', 1)[-1] == 'tGuias']
        if codigo not in ('0', '3') or (codigo == '3' and registros) or len(registros) != consulta.get('guias'):
            raise RuntimeError('Resposta externa inválida; não é possível considerar ausência de guia.')
        quando = datetime.fromisoformat(consulta['consultado_em'])
        if quando.tzinfo is None or quando.timestamp() > time.time() + 60:
            raise RuntimeError('Data da consulta externa inválida.')
        sincronizacoes.append((*par, quando.timestamp(), digest))
        for guia in registros:
            chave = texto(guia, 'chave_nfe').removeprefix('NFe')
            cnpj = texto(guia, 'emitente')
            receita = re.sub(r'[^0-9]', '', texto(guia, 'receita'))
            uf = texto(guia, 'uf_destinatario').upper()
            if not re.fullmatch(r'[0-9]{6}[A-Z0-9]{12}[0-9]{26}', chave) or chave[6:20] != cnpj:
                raise RuntimeError('Guia externa sem chave ou emitente confiável; revisão necessária.')
            if cnpj != par[0] or uf not in UFS or not re.fullmatch(r'[0-9]{6}', receita):
                raise RuntimeError('Guia externa com emitente, UF ou receita incompatível.')
            identidade = '|'.join((cnpj, uf, receita, chave))
            guias.append((identidade, texto(guia, 'num_controle') or texto(guia, 'recibo'), par[1], quando.timestamp(), digest))
    if vistas != esperadas:
        raise RuntimeError('Faltam consultas de autorizadas ou pendentes para um dos CNPJs.')
    return sincronizacoes, guias


def exportar_indice(pasta, destino, cnpjs):
    """Extrai só os identificadores necessários ao bloqueio, sem dados do destinatário."""
    sincronizacoes, guias = ler_historico(pasta, cnpjs)
    documento = {'versao': 1, 'cnpjs': sorted(set(cnpjs)),
                 'consultas': [dict(zip(('cnpj', 'operacao', 'consultado_em', 'fonte_sha256'), s)) for s in sincronizacoes],
                 'guias': [dict(zip(('identidade', 'controle', 'operacao', 'consultado_em', 'fonte_sha256'), g)) for g in guias]}
    conteudo = (json.dumps(documento, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    with Path(destino).open('xb') as arquivo:
        arquivo.write(conteudo)
    return hashlib.sha256(conteudo).hexdigest()


def ler_indice(caminho, cnpjs, sha256):
    conteudo = Path(caminho).read_bytes()
    if not sha256 or hashlib.sha256(conteudo).hexdigest() != sha256.lower():
        raise RuntimeError('O índice exige o SHA-256 conferido na origem.')
    documento = json.loads(conteudo)
    if documento.get('versao') != 1 or documento.get('cnpjs') != sorted(set(cnpjs)):
        raise RuntimeError('Versão ou CNPJs incompatíveis no índice de histórico.')
    esperadas = {(c, op) for c in cnpjs for op in OPERACOES}
    fontes, sincronizacoes, guias = {}, [], []
    for consulta in documento['consultas']:
        par = (consulta['cnpj'], consulta['operacao'])
        quando, digest = consulta['consultado_em'], consulta['fonte_sha256']
        if par not in esperadas or par in fontes or not isinstance(quando, (int, float)) or not 0 < quando <= time.time() + 60:
            raise RuntimeError('Consulta incompleta, repetida ou inválida no índice.')
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RuntimeError('Hash de origem inválido no índice.')
        fontes[par] = (quando, digest)
        sincronizacoes.append((*par, quando, digest))
    if set(fontes) != esperadas:
        raise RuntimeError('Faltam consultas de autorizadas ou pendentes no índice.')
    for guia in documento['guias']:
        partes = guia['identidade'].split('|')
        if len(partes) != 4:
            raise RuntimeError('Identidade de guia inválida no índice.')
        cnpj, uf, receita, chave = partes
        if cnpj not in cnpjs or uf not in UFS or not re.fullmatch(r'[0-9]{6}', receita) or not re.fullmatch(r'[0-9]{6}[A-Z0-9]{12}[0-9]{26}', chave) or chave[6:20] != cnpj:
            raise RuntimeError('Emitente, UF, receita ou chave incompatível no índice.')
        par = (cnpj, guia['operacao'])
        if par not in fontes or fontes[par] != (guia['consultado_em'], guia['fonte_sha256']):
            raise RuntimeError('Guia sem vínculo com a consulta de origem no índice.')
        guias.append((guia['identidade'], guia['controle'], guia['operacao'], guia['consultado_em'], guia['fonte_sha256']))
    return sincronizacoes, guias


def importar_historico(pasta, sha256=None):
    with closing(abrir_controle()) as con:
        politica = validar_base(PASTA_PRODUCAO, con)
        cnpjs = json.loads(politica['cnpjs'])
        sincronizacoes, guias = (ler_indice(pasta, cnpjs, sha256) if Path(pasta).is_file()
                                else ler_historico(pasta, cnpjs))
        with con:
            con.execute('BEGIN IMMEDIATE')
            # O desaparecimento de uma guia da API nunca apaga a reserva histórica.
            con.executemany('''INSERT INTO historico_externo VALUES(?,?,?,?,?)
                ON CONFLICT(identidade,controle,operacao) DO UPDATE SET
                consultado_em=max(consultado_em,excluded.consultado_em)''', guias)
            con.executemany('''INSERT INTO sincronizacoes VALUES(?,?,?,?)
                ON CONFLICT(cnpj,operacao) DO UPDATE SET consultado_em=excluded.consultado_em,
                fonte_sha256=excluded.fonte_sha256 WHERE excluded.consultado_em>=consultado_em''', sincronizacoes)
        return len(guias)


def autorizar_piloto(conteudo, chaves_origem=None, minutos=120):
    """Permissão expiráveis para o conteúdo exato de uma única NF-e; não envia."""
    if not 1 <= minutos <= 120:
        raise RuntimeError('O piloto deve expirar entre 1 e 120 minutos.')
    from emitir_gnre import identificar_lote
    cnpj, origens = identificar_lote(conteudo, chaves_origem)
    if len({i.rsplit('|', 1)[-1] for i in origens.values()}) != 1:
        raise RuntimeError('Autorize apenas uma NF-e por piloto.')
    if any(i.split('|')[2] not in ('100102', '100129') for i in origens.values()):
        raise RuntimeError('O piloto atende somente DIFAL e FCP do DIFAL.')
    with closing(abrir_controle()) as con, con:
        politica = validar_base(PASTA_PRODUCAO, con)
        if cnpj not in json.loads(politica['cnpjs']):
            raise RuntimeError('CNPJ não permitido no controle de produção.')
        _verificar_historico(con, politica, origens)
        con.execute('CREATE TABLE IF NOT EXISTS pilotos_producao('
                    'sha256 TEXT PRIMARY KEY, origens TEXT NOT NULL, criado_em REAL NOT NULL,'
                    'expira_em REAL NOT NULL, revogado INTEGER NOT NULL DEFAULT 0)')
        digest = hashlib.sha256(conteudo).hexdigest()
        con.execute('INSERT INTO pilotos_producao VALUES(?,?,?,?,0) ON CONFLICT(sha256) DO UPDATE SET '
                    'origens=excluded.origens,criado_em=excluded.criado_em,expira_em=excluded.expira_em,revogado=0',
                    (digest, json.dumps(origens, sort_keys=True), time.time(), time.time() + minutos * 60))
    return digest


def verificar_envio(con, pasta, origens, conteudo=None):
    politica = validar_base(pasta, con)
    if not politica['fluxo_exclusivo']:
        piloto = None
        if conteudo is not None and con.execute("SELECT 1 FROM sqlite_master WHERE name='pilotos_producao'").fetchone():
            piloto = con.execute('SELECT * FROM pilotos_producao WHERE sha256=?',
                                 (hashlib.sha256(conteudo).hexdigest(),)).fetchone()
        if not piloto or piloto['revogado'] or not piloto['criado_em'] <= time.time() < piloto['expira_em'] or json.loads(piloto['origens']) != origens:
            raise RuntimeError('Produção bloqueada: confirme o fluxo exclusivo ou autorize este lote exato para um piloto.')
    _verificar_historico(con, politica, origens)


def _verificar_historico(con, politica, origens):
    cnpjs = set(json.loads(politica['cnpjs']))
    for identidade in origens.values():
        cnpj = identidade.split('|')[0]
        if cnpj not in cnpjs:
            raise RuntimeError('CNPJ não permitido no controle de produção.')
        if con.execute('SELECT 1 FROM historico_externo WHERE identidade=?', (identidade,)).fetchone():
            raise RuntimeError('Já existe guia autorizada ou pendente no GuiasGNRE para esta nota e receita.')
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='encaminhamentos_manuais'").fetchone() and con.execute(
                'SELECT 1 FROM encaminhamentos_manuais WHERE identidade=?', (identidade,)).fetchone():
            raise RuntimeError('Nota encaminhada ao portal para tratamento manual; emissão automática bloqueada.')
        for operacao in OPERACOES:
            registro = con.execute('SELECT consultado_em FROM sincronizacoes WHERE cnpj=? AND operacao=?',
                                   (cnpj, operacao)).fetchone()
            if registro is None or not 0 <= time.time() - registro[0] <= VALIDADE_HISTORICO:
                raise RuntimeError('Atualize o histórico do GuiasGNRE antes de emitir: consulta ausente ou vencida.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='comando', required=True)
    inicial = sub.add_parser('inicializar')
    inicial.add_argument('--cnpj', action='append', required=True)
    importar = sub.add_parser('importar-historico')
    importar.add_argument('--pasta', type=Path, required=True)
    importar.add_argument('--sha256', help='Hash conferido na origem, obrigatório para importar índice reduzido')
    exportar = sub.add_parser('exportar-indice')
    exportar.add_argument('--pasta', type=Path, required=True)
    exportar.add_argument('--destino', type=Path, required=True)
    exportar.add_argument('--cnpj', action='append', required=True)
    ativar = sub.add_parser('confirmar-transicao')
    ativar.add_argument('--confirmar-fluxo-exclusivo', action='store_true', required=True)
    sub.add_parser('bloquear')
    piloto = sub.add_parser('autorizar-piloto')
    piloto.add_argument('--lote', type=Path, required=True)
    piloto.add_argument('--chaves-origem', type=Path)
    piloto.add_argument('--minutos', type=int, default=120)
    piloto.add_argument('--confirmar-nota-fora-do-portal', action='store_true', required=True)
    piloto.add_argument('--confirmar-situacao-atual-nfe', action='store_true', required=True)
    args = parser.parse_args()
    try:
        if args.comando == 'inicializar':
            inicializar(args.cnpj)
            print('Controle fixo inicializado. Emissão de produção continua bloqueada.')
        elif args.comando == 'importar-historico':
            print(f'Histórico importado: {importar_historico(args.pasta, args.sha256)} guias. Nenhuma guia emitida.')
        elif args.comando == 'exportar-indice':
            print(f'Índice reduzido preparado. SHA-256: {exportar_indice(args.pasta, args.destino, args.cnpj)}')
        elif args.comando == 'autorizar-piloto':
            chaves = json.loads(args.chaves_origem.read_text()) if args.chaves_origem else None
            print('Piloto autorizado para o lote exato: ' + autorizar_piloto(args.lote.read_bytes(), chaves, args.minutos))
            print('Transição geral preservada. Nenhuma guia emitida.')
        else:
            with closing(abrir_controle()) as con, con:
                con.execute('UPDATE politica_producao SET fluxo_exclusivo=? WHERE id=1',
                            (int(args.comando == 'confirmar-transicao'),))
                if args.comando == 'bloquear' and con.execute("SELECT 1 FROM sqlite_master WHERE name='pilotos_producao'").fetchone():
                    con.execute('UPDATE pilotos_producao SET revogado=1')
            print('Política atualizada. Nenhuma guia emitida.')
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as e:
        print(f'Falha no controle de produção: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    os.umask(0o077)
    sys.exit(main())
