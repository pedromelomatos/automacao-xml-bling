"""Cliente do desktop: RPC SSH restrito, sem certificado A1 ou chave administrativa."""

import base64
from datetime import date, datetime, time as horario, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
import zipfile


def carregar_configuracao(caminho):
    caminho = Path(caminho)
    config = json.loads(caminho.read_text(encoding='utf-8'))
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', config.get('servidor', '')):
        raise RuntimeError('Servidor GNRE inválido.')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', config.get('usuario', '')):
        raise RuntimeError('Usuário GNRE inválido.')
    porta = config.get('porta', 22)
    if type(porta) is not int or not 1 <= porta <= 65535:
        raise RuntimeError('Porta GNRE inválida.')
    for campo in ('chave', 'known_hosts'):
        arquivo = Path(config[campo]).expanduser()
        if not arquivo.is_absolute():
            arquivo = caminho.parent / arquivo
        if not arquivo.is_file():
            raise RuntimeError(f'Arquivo de acesso GNRE não encontrado ({campo}).')
        config[campo] = str(arquivo.resolve())
    config['porta'] = porta
    return config


class ClienteGNRE:
    def __init__(self, configuracao):
        self.configuracao = carregar_configuracao(configuracao)

    def pedir(self, acao, **dados):
        config = self.configuracao
        ssh = shutil.which('ssh')
        if not ssh:
            raise RuntimeError('Cliente OpenSSH não encontrado neste computador.')
        args = [ssh, '-F', 'NUL' if os.name == 'nt' else '/dev/null', '-T',
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + config['known_hosts'],
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                '-i', config['chave'], '-p', str(config['porta']),
                config['usuario'] + '@' + config['servidor'], 'gnre-desktop']
        entrada = json.dumps({'acao': acao, **dados}, ensure_ascii=False).encode()
        if len(entrada) > 12 * 1024 * 1024:
            raise RuntimeError('Pedido acima do limite de tamanho.')
        try:
            processo = subprocess.run(args, input=entrada, capture_output=True, timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired as erro:
            raise RuntimeError('A conexão demorou. Use Retomar acompanhamento para conferir o mesmo pedido.') from erro
        if processo.returncode:
            raise RuntimeError('Não foi possível acessar o Debian com esta credencial/rede. ' +
                               processo.stderr.decode('utf-8', errors='replace')[:600])
        if len(processo.stdout) > 28 * 1024 * 1024:
            raise RuntimeError('Resposta GNRE acima do limite de tamanho.')
        try:
            resposta = json.loads(processo.stdout)
        except (ValueError, UnicodeError) as erro:
            raise RuntimeError('Resposta GNRE inválida. Confira o pedido antes de outra tentativa.') from erro
        if not resposta.get('ok'):
            raise RuntimeError(resposta.get('erro') or 'Pedido recusado pelo Debian.')
        return resposta['resultado']


def ler_arquivos(caminhos):
    arquivos, vistos, total = [], set(), 0
    for caminho in map(Path, caminhos):
        if caminho.suffix.lower() == '.zip':
            with zipfile.ZipFile(caminho) as z:
                entradas = [(i.filename, i.file_size, lambda i=i: z.read(i))
                            for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith('.xml')]
                for nome, tamanho, ler in entradas:
                    if tamanho > 512 * 1024 or len(arquivos) >= 50 or total + tamanho > 8 * 1024 * 1024:
                        raise RuntimeError('Selecione no máximo 50 XMLs, com até 512 KiB cada e 8 MiB no total.')
                    xml = ler()
                    digest = hashlib.sha256(xml).hexdigest()
                    if digest not in vistos:
                        vistos.add(digest)
                        total += len(xml)
                        arquivos.append({'nome': nome.replace('\\', '/').rsplit('/', 1)[-1],
                                         'xml': base64.b64encode(xml).decode()})
        elif caminho.suffix.lower() == '.xml':
            tamanho = caminho.stat().st_size
            if tamanho > 512 * 1024 or len(arquivos) >= 50 or total + tamanho > 8 * 1024 * 1024:
                raise RuntimeError('Selecione no máximo 50 XMLs, com até 512 KiB cada e 8 MiB no total.')
            xml = caminho.read_bytes()
            digest = hashlib.sha256(xml).hexdigest()
            if digest not in vistos:
                vistos.add(digest)
                total += len(xml)
                arquivos.append({'nome': caminho.name, 'xml': base64.b64encode(xml).decode()})
        else:
            raise RuntimeError('Selecione arquivos XML ou ZIP.')
    if not arquivos:
        raise RuntimeError('Nenhum XML encontrado na seleção.')
    return arquivos


def revalidar_bling(notas):
    from baixar_xmls import ClienteBling, consultar_nfes
    from bling_auth import obter_access_token
    if not notas:
        raise RuntimeError('Nenhuma nota selecionada para conferir no Bling.')
    datas = [date.fromisoformat(n['emissao']) for n in notas]
    cliente = ClienteBling(obter_access_token())
    try:
        atuais = consultar_nfes(cliente, datetime.combine(min(datas), horario.min),
                                datetime.combine(max(datas) + timedelta(days=1), horario.min))
        por_chave = {str(n.get('chaveAcesso')): n for n in atuais}
        resultado = []
        for nota in notas:
            atual = por_chave.get(nota['chave'])
            if atual is None or str(atual.get('situacao')) not in ('5', '6'):
                raise RuntimeError(f"NF-e {nota['numero']}: situação atual no Bling fora do escopo ou nota não localizada.")
            resultado.append({'chave': nota['chave'], 'situacao': str(atual['situacao']),
                              'consultado_em': time.time()})
        return resultado
    finally:
        cliente.close()


def salvar_pdf(resposta, destino):
    nome = resposta['nome']
    if not re.fullmatch(r'GNRE-[A-Z0-9]{14}-NF-[0-9]+-[A-Z]{2}\.pdf', nome):
        raise RuntimeError('Nome de PDF inválido.')
    pdf = base64.b64decode(resposta['conteudo'], validate=True)
    if hashlib.sha256(pdf).hexdigest() != resposta['sha256'] or not pdf.startswith(b'%PDF-'):
        raise RuntimeError('PDF recebido com integridade inválida.')
    pasta = Path(destino)
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / nome
    if arquivo.exists():
        if arquivo.read_bytes() == pdf:
            return arquivo
        raise RuntimeError('Já existe outro PDF com esse nome; escolha outra pasta.')
    temporario = pasta / ('.gnre-' + uuid.uuid4().hex)
    try:
        temporario.write_bytes(pdf)
        temporario.replace(arquivo)
    finally:
        temporario.unlink(missing_ok=True)
    return arquivo
