"""Instala a identidade TLS da GNRE no Debian, com modo de ensaio isolado."""

import argparse
import getpass
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import warnings


SENHA_ENSAIO = b"gnre-teste-123"
BLOCOS_PEM = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----\r?\n.*?-----END \1-----", re.DOTALL)


def openssl(*argumentos, entrada=b""):
    return subprocess.run(["openssl", *argumentos], input=entrada, capture_output=True, check=False)


def executar_openssl(*argumentos, entrada=b""):
    resultado = openssl(*argumentos, entrada=entrada)
    if resultado.returncode:
        raise RuntimeError(f"Falha ao executar OpenSSL ({argumentos[0]}).")
    return resultado.stdout


def escolher_pfx(downloads_ensaio):
    downloads = downloads_ensaio if downloads_ensaio is not None else Path.home() / "Downloads"
    extensoes = (".pfx", ".p12")
    arquivos = sorted(
        caminho for caminho in downloads.rglob("*")
        if caminho.suffix.lower() in extensoes and not caminho.is_symlink() and caminho.is_file()
    )
    padrao = arquivos[0] if len(arquivos) == 1 else None
    opcoes = {}
    sufixo = f" [{padrao.name}]" if padrao else ""
    if len(arquivos) > 1:
        print("Certificados encontrados em Downloads")
        for numero, caminho in enumerate(arquivos, start=1):
            opcoes[str(numero)] = caminho
            print(f"  {numero}. {caminho.relative_to(downloads)}")
        sufixo = " (número da lista ou caminho)"

    while True:
        texto = input(f"Arquivo PFX{sufixo}: ").strip()
        if padrao and texto in ("", padrao.name):
            return padrao
        if texto in opcoes:
            return opcoes[texto]
        caminho = Path(texto).expanduser() if texto else None
        if downloads_ensaio is not None:
            if caminho in arquivos:
                return caminho
        elif caminho and caminho.suffix.lower() in extensoes and caminho.is_file():
            return caminho.resolve()
        if padrao:
            print("Pressione Enter para usar o arquivo selecionado ou confira o caminho informado.")
        elif opcoes:
            print("Escolha um número da lista ou informe o caminho de um PFX.")
        else:
            print("Informe o caminho de um arquivo .pfx ou .p12 existente.")


def abrir_pfx(caminho):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        while True:
            senha = getpass.getpass("Senha do PFX: ")
            if any(ord(caractere) < 32 or caractere == "\x7f" for caractere in senha):
                del senha
                print("A entrada contém caracteres de controle. Digite a senha novamente.")
                continue
            argumentos = ("pkcs12", "-in", str(caminho), "-noenc", "-passin", "stdin")
            entrada = senha.encode("utf-8") + b"\n"
            del senha
            try:
                resultado = openssl(*argumentos, entrada=entrada)
                if resultado.returncode and b"unsupported" in resultado.stderr.lower():
                    resultado = openssl(*argumentos, "-legacy", entrada=entrada)
            finally:
                del entrada
            if not resultado.returncode:
                return resultado.stdout
            if b"invalid password" not in resultado.stderr.lower():
                raise RuntimeError("O OpenSSL não conseguiu abrir o PFX. Confira o arquivo e o formato.")
            print("Senha incorreta. Tente novamente.")


def validar_identidade(conteudo):
    chaves = []
    certificados = []
    for bloco in BLOCOS_PEM.finditer(conteudo):
        pem = bloco[0].replace(b"\r\n", b"\n") + b"\n"
        if bloco[1].endswith(b"PRIVATE KEY"):
            chaves.append(pem)
        elif bloco[1] == b"CERTIFICATE":
            certificados.append(pem)
    if len(chaves) != 1:
        raise RuntimeError("O PFX deve conter uma única chave privada.")

    chave = chaves[0]
    publica = executar_openssl("pkey", "-pubout", entrada=chave)
    certificados = list(dict.fromkeys(certificados))
    for certificado in certificados:
        if executar_openssl("x509", "-noout", "-pubkey", entrada=certificado) == publica:
            break
    else:
        raise RuntimeError("O PFX não contém o certificado correspondente à chave privada.")

    dados = executar_openssl(
        "x509", "-noout", "-subject", "-nameopt", "utf8,-esc_msb", "-dates", entrada=certificado
    ).decode("utf-8")
    campos = dict(linha.split("=", 1) for linha in dados.splitlines())
    inicio = ssl.cert_time_to_seconds(campos["notBefore"])
    fim = ssl.cert_time_to_seconds(campos["notAfter"])
    if not inicio <= time.time() < fim:
        raise RuntimeError("O certificado está fora do período de validade.")

    cadeia = b"".join(outro for outro in certificados if outro != certificado)
    return chave + certificado + cadeia, campos


def instalar_identidade(pem, destino):
    pasta = destino.parent
    pasta.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        dados = pasta.lstat()
        if not stat.S_ISDIR(dados.st_mode) or dados.st_uid != os.geteuid() or dados.st_mode & 0o077:
            raise RuntimeError(f"A pasta deve pertencer ao usuário atual e ter permissão 700: {pasta}")

    temporario = None
    try:
        with tempfile.NamedTemporaryFile(dir=pasta, prefix=".identidade-", delete=False) as arquivo:
            temporario = Path(arquivo.name)
            arquivo.write(pem)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_cert_chain(temporario)
        os.replace(temporario, destino)
    finally:
        if temporario:
            temporario.unlink(missing_ok=True)


def instalar(destino, downloads_ensaio=None):
    pem, dados = validar_identidade(abrir_pfx(escolher_pfx(downloads_ensaio)))
    print(f"Titular: {dados['subject'].strip()}")
    print(f"Validade: {dados['notBefore']} a {dados['notAfter']}")
    acao = "Substituir o certificado instalado" if destino.exists() else "Confirmar instalação"
    if input(f"{acao}? [s/N] ").strip().lower() != "s":
        print("Instalação cancelada.")
        return False
    instalar_identidade(pem, destino)
    print("Certificado instalado.", flush=True)
    return True


def gerar_pfx_ensaio(pasta):
    chave = pasta / "cliente.key"
    certificado = pasta / "cliente.crt"
    pfx = pasta / "CERTIFICADO DIGITAL MATRIZ.pfx"
    executar_openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-noenc", "-days", "1",
        "-subj", "/CN=Empresa Modelo Ltda",
        "-addext", "basicConstraints=critical,CA:FALSE",
        "-addext", "extendedKeyUsage=clientAuth", "-keyout", str(chave), "-out", str(certificado),
    )
    executar_openssl(
        "pkcs12", "-export", "-inkey", str(chave), "-in", str(certificado), "-out", str(pfx),
        "-passout", "stdin", entrada=SENHA_ENSAIO + b"\n",
    )
    chave.unlink()
    return pfx


def ensaiar():
    with tempfile.TemporaryDirectory(prefix="gnre-ensaio-") as diretorio:
        pasta = Path(diretorio)
        downloads = pasta / "Downloads"
        subpasta = downloads / "Certificados"
        subpasta.mkdir(parents=True)
        pfx = gerar_pfx_ensaio(subpasta)
        destino = pasta / "instalacao" / "identidade.pem"
        if not instalar(destino, downloads):
            return False
        pfx.unlink()
        subprocess.run(
            [
                sys.executable, "-I", "-c",
                "import ssl, sys; ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_cert_chain(sys.argv[1])",
                str(destino),
            ],
            check=True,
        )
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modo = parser.add_mutually_exclusive_group(required=True)
    modo.add_argument("--ensaio", action="store_true", help="usa um PFX fictício e uma pasta temporária")
    modo.add_argument("--instalar", action="store_true", help="instala um PFX no perfil do usuário no Debian")
    args = parser.parse_args()

    if not shutil.which("openssl"):
        parser.error("OpenSSL 3 é necessário. Confira com openssl version.")
    if args.instalar and sys.platform != "linux":
        parser.error("a instalação real é destinada ao Debian; use --ensaio neste sistema")
    if args.instalar and os.geteuid() == 0:
        parser.error("execute com o usuário da automação, sem sudo")

    resumo_ensaio = "Esta execução foi um ensaio com arquivos temporários."
    try:
        print("=== Instalador GNRE ===", flush=True)
        if args.ensaio:
            if ensaiar():
                resumo_ensaio = "Um novo processo abriu a credencial fictícia sem pedir a senha."
        else:
            pasta = Path.home() / ".local" / "share" / "automacao-xml-bling" / "gnre"
            instalar(pasta / "identidade.pem")
    except getpass.GetPassWarning:
        print("Use um terminal interativo para que a senha fique oculta.", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nInstalação interrompida.", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, subprocess.CalledProcessError) as erro:
        print(f"Falha: {erro}", file=sys.stderr)
        return 1
    finally:
        if args.ensaio:
            print(f"[TESTE] {resumo_ensaio}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
