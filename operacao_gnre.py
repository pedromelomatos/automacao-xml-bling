"""Entrega, retomada por recibo e backup/restauração GNRE. Nunca transmite lotes."""

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile

import emitir_gnre as emissor
from conferir_pagamento_gnre import conferir_codigo, conferir_pdf


def json_bytes(valor):
    return (json.dumps(valor, ensure_ascii=False, indent=2) + '\n').encode()


def abrir_existente(base, leitura=False):
    banco = Path(base).resolve() / 'controle.sqlite3'
    if banco.is_symlink() or not banco.is_file():
        raise RuntimeError('Informe uma base GNRE existente, sem links.')
    con = sqlite3.connect(banco.as_uri() + ('?mode=ro' if leitura else '?mode=rw'), uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def pasta_lote(base, lote):
    base = Path(base).resolve()
    pasta = Path(lote['pasta'])
    # O volume Docker /dados/gnre corresponde à raiz do próprio controle.
    # Somente aceitar esse remapeamento com prova do XML original no host.
    if pasta.is_relative_to('/dados/gnre') and not pasta.resolve().is_relative_to(base):
        candidata = base / pasta.relative_to('/dados/gnre')
        if (candidata / 'lote.xml').is_file() and (candidata / 'lote.xml').read_bytes() == lote['xml']:
            pasta = candidata
    if not pasta.is_absolute() or not pasta.resolve().is_relative_to(base):
        raise RuntimeError('Pasta do lote fora da base. Use a base original/host ou restaure com remapeamento.')
    relativa = pasta.relative_to(base)
    if any((base / Path(*relativa.parts[:i])).is_symlink() for i in range(1, len(relativa.parts) + 1)):
        raise RuntimeError('Pasta de lote contém link; cópia interrompida.')
    return pasta


def entregar(base, lote_id, destino, hoje=None):
    hoje = hoje or datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    if (Path(base) / 'RESTAURADO_BLOQUEADO.json').exists():
        raise RuntimeError('Cópia restaurada bloqueada: reconferir offline antes de adotar a base.')
    with closing(abrir_existente(base, True)) as con:
        lote = con.execute('SELECT * FROM lotes WHERE id=?', (lote_id,)).fetchone()
        if lote is None or lote['estado'] != 'concluido':
            raise RuntimeError('Somente lotes concluídos podem ser entregues.')
        pasta = pasta_lote(base, lote)
        resumo = json.loads((pasta / 'resumo.json').read_text(encoding='utf-8'))
        if lote['ambiente'] != 'producao' or not resumo.get('liberado_para_pagamento'):
            raise RuntimeError('Entrega de pagamento exige guia de produção liberada.')
        if resumo.get('resultado_sha256') != hashlib.sha256(lote['resultado']).hexdigest():
            raise RuntimeError('Resumo não corresponde ao resultado persistido; reconferir offline.')
        if not resumo.get('conferencia_pdf', {}).get('representacao_numerica_conferida'):
            raise RuntimeError('PDF ainda não passou pela conferência de pagamento.')
        origens = json.loads(lote['origens'])
        guias = resumo.get('guias', [])
        if {g['identificador']: g.get('origem') for g in guias} != origens:
            raise RuntimeError('Origens da entrega diferem do controle contra duplicidade.')
        pagamentos = []
        for g in guias:
            conferencia = g.get('conferencia', {})
            if g['situacao'] != '0' or conferencia.get('divergencias'):
                raise RuntimeError('Guia rejeitada ou com divergências não pode ser entregue.')
            limite = conferencia.get('validade') or conferencia.get('vencimento')
            if not limite or date.fromisoformat(limite) < hoje:
                raise RuntimeError('Validade encerrada. Conferir a guia; não reemitir automaticamente.')
            cnpj, uf, receita, chave = g['origem'].split('|')
            pagamentos.append({**conferir_codigo(g['codigo_barras'], g['linha_digitavel'], conferencia['total']),
                               'cnpj': cnpj, 'uf': uf, 'receita': receita, 'chave': chave,
                               'vencimento': conferencia['vencimento'], 'validade': conferencia['validade']})
        if not pagamentos or len({g['cnpj'] for g in pagamentos}) != 1:
            raise RuntimeError('Entrega vazia ou com mais de um emitente.')
        pdf_nome = f"guias-{lote['recibo']}.pdf"
        pdf = (pasta / pdf_nome).read_bytes()
        conferencia_pdf = conferir_pdf(pdf, pagamentos, 'producao')
        if conferencia_pdf['sha256'] != resumo['conferencia_pdf'].get('sha256'):
            raise RuntimeError('PDF foi alterado após a conferência.')
        manifesto = {'versao': 1, 'lote': lote_id, 'recibo': lote['recibo'], 'ambiente': 'producao',
                     'cnpj': pagamentos[0]['cnpj'], 'data_entrega': hoje.isoformat(),
                     'pagamento': 'nao_conciliado', 'pdf': pdf_nome,
                     'pdf_sha256': conferencia_pdf['sha256'], 'guias': pagamentos}
    saida = Path(destino).resolve() / manifesto['cnpj'] / hoje.isoformat() / lote_id
    if saida.exists():
        if (saida / 'manifesto.json').read_bytes() == json_bytes(manifesto) and (saida / pdf_nome).read_bytes() == pdf:
            return saida
        raise RuntimeError('Entrega anterior diverge; nenhum arquivo substituído.')
    saida.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.entrega-', dir=saida.parent) as tmp:
        trabalho = Path(tmp) / 'pronto'
        trabalho.mkdir(mode=0o700)
        emissor.gravar(trabalho / pdf_nome, pdf)
        emissor.gravar(trabalho / 'manifesto.json', json_bytes(manifesto))
        texto = ['GNRE - documentos conferidos para pagamento', f"CNPJ: {manifesto['cnpj']}",
                 f"Recibo: {manifesto['recibo']}", 'Pagamento ainda não conciliado.']
        for g in pagamentos:
            texto += [f"{g['uf']} / {g['receita']} - R$ {g['total']} - validade: {g['validade'] or g['vencimento']}",
                      f"Linha digitável: {g['linha_digitavel']}"]
        emissor.gravar(trabalho / 'LEIA-ME.txt', ('\n'.join(texto) + '\n').encode())
        trabalho.rename(saida)
    return saida


def retomar(base, servico, ambiente, limite=10):
    """Uma rodada limitada, respeitando intervalos e leases. Sem chamar receber()."""
    if not 1 <= limite <= 100:
        raise ValueError('Limite de consultas deve estar entre 1 e 100.')
    respostas = []
    with closing(abrir_existente(base)) as con:
        lotes = con.execute("SELECT * FROM lotes WHERE ambiente=? AND recibo IS NOT NULL "
                            "AND estado IN ('recebido','pendente') AND proxima_consulta<=? "
                            "AND consultando_ate<=? ORDER BY proxima_consulta,id LIMIT ?",
                            (ambiente, time.time(), time.time(), limite)).fetchall()
        for lote in lotes:
            pasta_lote(base, lote)
            try:
                emissor.consultar_lote(con, lote, servico)
                erro = None
            except (OSError, RuntimeError, ValueError, sqlite3.Error, http.client.HTTPException) as exc:
                erro = str(exc)
                # Erro transitório de consulta nunca autoriza novo envio.
                limitado = getattr(exc, 'status', None) == 429
                espera = max(300, getattr(exc, 'repetir_apos', 0))
                with con:
                    con.execute('UPDATE lotes SET proxima_consulta=MAX(proxima_consulta,?),erro=? WHERE id=?',
                                (time.time() + espera, erro, lote['id']))
                    if limitado:
                        con.execute("UPDATE lotes SET proxima_consulta=MAX(proxima_consulta,?) WHERE ambiente=? "
                                    "AND estado IN ('recebido','pendente')", (time.time() + espera, ambiente))
            estado = con.execute('SELECT estado FROM lotes WHERE id=?', (lote['id'],)).fetchone()[0]
            respostas.append({'lote': lote['id'], 'recibo': lote['recibo'], 'estado': estado, 'erro': erro})
            if erro and limitado:
                break
    return respostas


def criar_backup(base, destino):
    """Snapshot SQLite e anexos conhecidos; exclui certificados/tokens/configurações."""
    base, destino = Path(base).resolve(), Path(destino).resolve()
    if destino.exists() or destino.is_relative_to(base):
        raise RuntimeError('Backup exige arquivo novo fora da base de dados.')
    destino.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.backup-gnre-', dir=destino.parent) as tmp:
        tmp = Path(tmp)
        with closing(abrir_existente(base)) as trava:
            trava.execute('BEGIN IMMEDIATE')
            try:
                with closing(abrir_existente(base, True)) as origem, closing(sqlite3.connect(tmp / 'controle.sqlite3')) as copia:
                    origem.backup(copia)
                    if copia.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Banco de backup falhou na verificação de integridade.')
                    copia.row_factory = sqlite3.Row
                    lotes = copia.execute('SELECT * FROM lotes ORDER BY id').fetchall()
                    for lote in lotes:
                        if not re.fullmatch('[0-9a-f]{64}', lote['id']):
                            raise RuntimeError('Identificador inválido no controle.')
                        fonte = pasta_lote(base, lote)
                        pasta = tmp / 'lotes' / lote['id']
                        pasta.mkdir(mode=0o700, parents=True)
                        for coluna, nome in (('xml', 'lote.xml'), ('recepcao', 'recepcao.xml'), ('resultado', 'resultado.xml')):
                            if lote[coluna]:
                                (pasta / nome).write_bytes(lote[coluna])
                        for arquivo in fonte.rglob('*') if fonte.exists() else ():
                            relativa = arquivo.relative_to(fonte)
                            if arquivo.is_symlink():
                                raise RuntimeError('Link em pasta fiscal; backup interrompido.')
                            if arquivo.is_file() and (relativa.as_posix() == 'resumo.json' or
                                re.fullmatch(r'(revisao/)?guias-[0-9]+\.pdf', relativa.as_posix())):
                                alvo = pasta / relativa
                                alvo.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                                shutil.copyfile(arquivo, alvo)
            finally:
                trava.rollback()
        arquivos = {p.relative_to(tmp).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(tmp.rglob('*')) if p.is_file()}
        manifesto = {'versao': 1, 'criado_em': datetime.now(timezone.utc).isoformat(),
                     'lotes': len(lotes), 'arquivos': arquivos}
        pacote = tmp / 'backup.zip'
        with zipfile.ZipFile(pacote, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('manifesto.json', json_bytes(manifesto))
            for nome in arquivos:
                z.write(tmp / nome, nome)
        os.chmod(pacote, 0o600)
        # A criação exclusiva evita substituir silenciosamente backup anterior.
        with destino.open('xb') as saida, pacote.open('rb') as entrada:
            os.chmod(destino, 0o600)
            shutil.copyfileobj(entrada, saida)
            saida.flush()
            os.fsync(saida.fileno())
    return manifesto


def restaurar_backup(arquivo, destino):
    """Restaura apenas para pasta nova, remapeia caminhos e bloqueia produção."""
    destino = Path(destino).absolute()
    if destino.exists() or destino.is_symlink():
        raise RuntimeError('Restauração exige pasta nova; nenhuma base existente será substituída.')
    destino.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.restauracao-gnre-', dir=destino.parent) as tmp:
        trabalho = Path(tmp) / 'restaurado'
        trabalho.mkdir(mode=0o700)
        with zipfile.ZipFile(arquivo) as z:
            nomes = z.namelist()
            if len(nomes) != len(set(nomes)) or sum(i.file_size for i in z.infolist()) > 512 * 1024 * 1024:
                raise RuntimeError('Backup repetido ou acima do limite de 512 MiB.')
            manifesto = json.loads(z.read('manifesto.json'))
            esperados = manifesto.get('arquivos', {})
            if manifesto.get('versao') != 1 or set(nomes) != set(esperados) | {'manifesto.json'}:
                raise RuntimeError('Manifesto do backup inválido.')
            for nome, sha in esperados.items():
                partes = PurePosixPath(nome)
                if partes.is_absolute() or '..' in partes.parts or '\\' in nome or ':' in nome:
                    raise RuntimeError('Caminho inseguro no backup.')
                if nome != 'controle.sqlite3' and not re.fullmatch(
                    r'lotes/[0-9a-f]{64}/(?:(?:lote|recepcao|resultado)\.xml|resumo\.json|(?:revisao/)?guias-[0-9]+\.pdf)', nome):
                    raise RuntimeError('Arquivo não permitido no backup.')
                dados = z.read(nome)
                if hashlib.sha256(dados).hexdigest() != sha:
                    raise RuntimeError('Hash de arquivo do backup inválido.')
                emissor.gravar(trabalho / nome, dados)
        with closing(abrir_existente(trabalho)) as con, con:
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Integridade do banco restaurado inválida.')
            for lote in con.execute('SELECT id FROM lotes').fetchall():
                if not re.fullmatch('[0-9a-f]{64}', lote['id']):
                    raise RuntimeError('Identificador de lote inválido.')
                con.execute('UPDATE lotes SET pasta=?,consultando_ate=0 WHERE id=?',
                            (str(destino / 'lotes' / lote['id']), lote['id']))
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='politica_producao'").fetchone():
                con.execute('UPDATE politica_producao SET fluxo_exclusivo=0,pasta=?', (str(destino),))
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='pilotos_producao'").fetchone():
                con.execute('UPDATE pilotos_producao SET revogado=1')
        emissor.gravar(trabalho / 'RESTAURADO_BLOQUEADO.json', json_bytes({
            'restaurado_em': datetime.now(timezone.utc).isoformat(), 'producao_bloqueada': True,
            'aviso': 'Cópia isolada. Reconferir retornos/PDFs antes de entrega. Não substituir o controle ativo.'}))
        trabalho.rename(destino)
    return manifesto


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='acao', required=True)
    entrega = sub.add_parser('entregar')
    entrega.add_argument('--base', type=Path, required=True)
    entrega.add_argument('--lote-id', required=True)
    entrega.add_argument('--destino', type=Path, required=True)
    backup = sub.add_parser('backup')
    backup.add_argument('--base', type=Path, required=True)
    backup.add_argument('--arquivo', type=Path, required=True)
    restaurar = sub.add_parser('restaurar')
    restaurar.add_argument('--arquivo', type=Path, required=True)
    restaurar.add_argument('--destino', type=Path, required=True)
    retoma = sub.add_parser('retomar')
    retoma.add_argument('--base', type=Path, required=True)
    retoma.add_argument('--identidade', type=Path, required=True)
    retoma.add_argument('--ambiente', choices=('producao', 'homologacao'), required=True)
    retoma.add_argument('--limite', type=int, default=10)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.acao == 'backup':
            valor = criar_backup(args.base, args.arquivo)
        elif args.acao == 'restaurar':
            valor = restaurar_backup(args.arquivo, args.destino)
        elif args.acao == 'entregar':
            valor = str(entregar(args.base, args.lote_id, args.destino))
        else:
            if (args.base / 'RESTAURADO_BLOQUEADO.json').exists():
                raise RuntimeError('Cópia restaurada bloqueada; adotar somente após conferência offline.')
            if args.ambiente == 'producao':
                emissor.controle_producao.validar_base(args.base)
            valor = retomar(args.base, emissor.ServicoGnre(args.identidade, args.ambiente), args.ambiente, args.limite)
        print(json.dumps(valor, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, zipfile.BadZipFile) as erro:
        parser.exit(1, f'Falha operacional GNRE: {erro}\n')


if __name__ == '__main__':
    raise SystemExit(main())
