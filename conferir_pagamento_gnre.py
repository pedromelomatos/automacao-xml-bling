"""Confere arrecadação FEBRABAN e o texto do PDF original, sem gerar guias.

Leiaute FEBRABAN versão 08, vigente desde 01/06/2026. O campo livre não é
interpretado como vencimento: seu significado depende do órgão arrecadador.
Poppler (pdftotext) é uma dependência operacional; sua ausência bloqueia a entrega.
"""

from decimal import Decimal
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata


def digito(numero, modulo):
    if modulo == 10:
        soma = sum(sum(map(int, str(int(n) * (2 if i % 2 == 0 else 1))))
                   for i, n in enumerate(reversed(numero)))
        return str((-soma) % 10)
    resto = sum(int(n) * (2 + i % 8) for i, n in enumerate(reversed(numero))) % 11
    return str(0 if resto in (0, 1) else 1 if resto == 10 else 11 - resto)


def numeros(valor, tamanho):
    if not isinstance(valor, str) or re.search(r'[^0-9\s.\-]', valor):
        raise ValueError('Código de pagamento contém caracteres inválidos.')
    numero = re.sub(r'[\s.\-]', '', valor)
    if len(numero) != tamanho:
        raise ValueError(f'Código de pagamento deve ter {tamanho} dígitos.')
    return numero


def conferir_codigo(codigo, linha, total):
    codigo, linha = numeros(codigo, 44), numeros(linha, 48)
    if codigo[:2] != '85' or codigo[2] not in '68':
        raise ValueError('Esperada arrecadação governamental com valor efetivo em reais (856/858).')
    modulo = 10 if codigo[2] == '6' else 11
    if codigo[3] != digito(codigo[:3] + codigo[4:], modulo):
        raise ValueError('Dígito verificador geral do código de barras inválido.')
    blocos = [linha[i:i + 12] for i in range(0, 48, 12)]
    if any(b[-1] != digito(b[:11], modulo) for b in blocos):
        raise ValueError('Dígito verificador de bloco da linha digitável inválido.')
    if ''.join(b[:11] for b in blocos) != codigo:
        raise ValueError('Linha digitável e código de barras representam guias diferentes.')
    valor = Decimal(codigo[4:15]) / 100
    if valor != Decimal(total) or valor <= 0:
        raise ValueError('Valor do código de barras difere do total retornado.')
    return {'codigo_barras': codigo, 'linha_digitavel': linha, 'total': f'{valor:.2f}', 'modulo': modulo}


def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFKD', texto.upper())
                   if not unicodedata.combining(c))


def extrair_texto(pdf):
    if len(pdf) > 16 * 1024 * 1024 or not pdf.startswith(b'%PDF-') or b'%%EOF' not in pdf[-2048:]:
        raise ValueError('PDF incompleto ou acima do limite de 16 MiB.')
    # Processo separado, prazo e número de páginas limitados; nunca executar ações do PDF.
    with tempfile.TemporaryDirectory(prefix='conferencia-gnre-') as pasta:
        entrada, saida = Path(pasta) / 'original.pdf', Path(pasta) / 'texto.txt'
        entrada.write_bytes(pdf)
        try:
            info = subprocess.run(['pdfinfo', str(entrada)], capture_output=True, timeout=15)
            paginas = re.search(rb'^Pages:\s+([0-9]+)', info.stdout, re.MULTILINE)
            if info.returncode or not paginas or not 1 <= int(paginas[1]) <= 100:
                raise ValueError('PDF ilegível ou acima do limite de 100 páginas.')
            retorno = subprocess.run(['pdftotext', '-layout', '-f', '1', '-l', '100',
                                      str(entrada), str(saida)], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as erro:
            raise ValueError('Não foi possível conferir o PDF com Poppler (pdftotext).') from erro
        if retorno.returncode or not saida.exists() or saida.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('PDF ilegível ou texto acima do limite.')
        texto = saida.read_text(encoding='utf-8')
        if not texto.strip():
            raise ValueError('PDF sem texto legível; conferir manualmente antes de qualquer pagamento.')
        return texto


def conferir_pdf(pdf, guias, ambiente):
    """Cada guia contém código/linha validados, CNPJ, UF, receita e datas do retorno.

    Confere representação numérica impressa. Não substitui teste de leitura das
    barras/QR pelo banco; o piloto real permanece necessário.
    """
    texto = normalizar(extrair_texto(pdf))
    compacto = re.sub(r'\s+', ' ', texto)
    invalido = 'GUIA INVALIDA' in compacto and 'PARA PAGAMENTO' in compacto
    if ambiente == 'producao' and (invalido or 'HOMOLOGACAO' in texto):
        raise ValueError('PDF de teste não pode ser liberado em produção.')
    if ambiente == 'homologacao' and not invalido:
        raise ValueError('PDF de homologação sem a identificação de guia inválida para pagamento.')
    blocos = re.split(r'(?=GUIA NACIONAL DE RECOLHIMENTO DE TRIBUTOS ESTADUAIS\s*-\s*GNRE)', texto)[1:]
    if not blocos:
        raise ValueError('PDF sem o formulário GNRE reconhecível.')
    linhas_encontradas = set()
    for bloco in blocos:
        for linha in bloco.splitlines():
            candidato = re.sub(r'[\s.\-]', '', linha)
            # A legenda da via pode estar na mesma linha, à direita do código.
            padrao = re.match(r'^\s*((?:[0-9][\s.\-]*){48})(?![0-9])', linha)
            if len(candidato) == 48 and candidato.isascii() and candidato.isdigit():
                linhas_encontradas.add(candidato)
            elif padrao:
                linhas_encontradas.add(re.sub(r'[\s.\-]', '', padrao[1]))
    esperadas = {g['linha_digitavel'] for g in guias}
    if len(esperadas) != len(guias) or linhas_encontradas != esperadas:
        raise ValueError('As linhas digitáveis impressas no PDF diferem das guias aceitas.')
    for guia in guias:
        blocos_guia = [b for b in blocos if guia['linha_digitavel'] in re.sub(r'[\s.\-]', '', b)]
        if not blocos_guia:
            raise ValueError('Linha digitável não localizada no formulário da guia.')
        # As cópias da mesma guia devem trazer a mesma identificação.
        for bloco in blocos_guia:
            if guia['cnpj'] not in re.sub(r'[^0-9A-Z]', '', bloco):
                raise ValueError('CNPJ do emitente não localizado no PDF da guia.')
            if not re.search(r'\b' + re.escape(guia['uf']) + r'\s+' + guia['receita'] + r'\b', bloco):
                raise ValueError('UF/receita do PDF difere do retorno.')
            for campo in ('vencimento', 'validade'):
                if guia.get(campo):
                    data = '/'.join(reversed(guia[campo].split('-')))
                    if data not in bloco:
                        raise ValueError(f'Data de {campo} não localizada no PDF da guia.')
    return {'sha256': hashlib.sha256(pdf).hexdigest(), 'linhas_conferidas': len(esperadas),
            'representacao_numerica_conferida': True, 'barras_opticas_conferidas': False,
            'homologacao': invalido}
