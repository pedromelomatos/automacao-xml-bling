"""Transporte explícito de XMLs pelo rclone, desabilitado por padrão.

A listagem é somente leitura. Este módulo não executa espelhamento, exclusão,
movimentação remota ou reenvio automático de uma transferência incerta.
"""

import hashlib
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


CHAVE = re.compile(r"(?<!\d)\d{44}(?!\d)")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*:")
COMPONENTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]*")
ARQUIVO_XML = re.compile(r"(?P<chave>\d{44})-nfe\.xml")
NS = {"n": "http://www.portalfiscal.inf.br/nfe"}


class ErroTransporte(RuntimeError):
    """Erro que não confirma uma entrega; nunca contém saída bruta do rclone."""


class ErroLeitura(ErroTransporte):
    """Inventário remoto indisponível ou inválido."""


class EntregaIncerta(ErroTransporte):
    """O processo de envio iniciou, mas seu resultado precisa de conciliação."""


class EnvioDesabilitado(ErroTransporte):
    """A configuração não permite iniciar uma operação de escrita."""


def validar_caminho_relativo(caminho):
    """Aceita apenas caminho POSIX relativo, sem segmentos ambíguos ou flags."""
    if not isinstance(caminho, str) or not caminho or "\\" in caminho:
        raise ErroTransporte("Caminho remoto relativo inválido.")
    if caminho.startswith("/") or ":" in caminho or "//" in caminho:
        raise ErroTransporte("Caminho remoto relativo inválido.")
    partes = caminho.split("/")
    if any(
        parte in {"", ".", ".."}
        or not COMPONENTE.fullmatch(parte)
        or parte.endswith((" ", "."))
        for parte in partes
    ):
        raise ErroTransporte("Caminho remoto relativo inválido.")
    return PurePosixPath(caminho).as_posix()


def validar_xml_completo(conteudo, chave_esperada):
    """Valida estrutura, chave e protocolo de autorização sem alterar os bytes."""
    try:
        raiz = ElementTree.fromstring(conteudo)
    except (ElementTree.ParseError, ValueError) as erro:
        raise ErroTransporte("Arquivo não contém XML completo e válido.") from erro
    if raiz.tag != f"{{{NS['n']}}}nfeProc":
        raise ErroTransporte("XML de entrega precisa conter NF-e e protocolo.")
    inf_nfe = raiz.find("n:NFe/n:infNFe", NS)
    protocolo = raiz.find("n:protNFe/n:infProt", NS)
    if inf_nfe is None or protocolo is None:
        raise ErroTransporte("XML de entrega precisa conter NF-e e protocolo.")
    if (
        inf_nfe.get("Id") != "NFe" + chave_esperada
        or protocolo.findtext("n:chNFe", namespaces=NS) != chave_esperada
        or protocolo.findtext("n:cStat", namespaces=NS) not in {"100", "150"}
    ):
        raise ErroTransporte("Chave ou autorização do XML diverge da entrega.")


class RcloneDropbox:
    def __init__(
        self, executavel, arquivo_config, remote="bling_dropbox:",
        timeout=120, permitir_envio=False,
    ):
        if not isinstance(remote, str) or not REMOTE.fullmatch(remote):
            raise ErroTransporte("Nome do remote rclone inválido.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ErroTransporte("Timeout do rclone precisa ser positivo e finito.")
        # Não aceitar strings como "false", que seriam verdadeiras em um if.
        if not isinstance(permitir_envio, bool):
            raise ErroTransporte("Permissão de envio precisa ser booleana.")
        self.executavel = str(executavel)
        self.arquivo_config = Path(arquivo_config)
        self.remote = remote
        self.timeout = timeout
        self.permitir_envio = permitir_envio

    def _validar_instalacao(self, classe_erro):
        if not self.arquivo_config.is_file():
            raise classe_erro("Configuração autorizada do rclone não encontrada.")
        if not self.executavel or not (
            Path(self.executavel).is_file() or shutil.which(self.executavel)
        ):
            raise classe_erro("Executável rclone não encontrado.")

    def _executar(self, argumentos):
        return subprocess.run(
            [self.executavel, *argumentos, "--config", str(self.arquivo_config)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            check=False,
            shell=False,
        )

    def listar_xmls(self):
        self._validar_instalacao(ErroLeitura)
        try:
            resultado = self._executar([
                "lsf", self.remote, "--recursive", "--files-only",
                "--include", "*.xml", "--include", "*.XML", "--format", "p",
                "--retries", "1", "--low-level-retries", "1",
            ])
        except (subprocess.TimeoutExpired, OSError, UnicodeError):
            raise ErroLeitura("Não foi possível concluir a listagem remota do Dropbox.") from None
        if resultado.returncode != 0:
            raise ErroLeitura("A listagem remota do Dropbox falhou.")
        inventario = {}
        for linha in resultado.stdout.splitlines():
            if not linha:
                continue
            try:
                caminho = validar_caminho_relativo(linha)
            except ErroTransporte:
                # Uma listagem parcial ou ambígua nunca deve autorizar novos envios.
                raise ErroLeitura("Inventário remoto contém caminho inválido; envio deve permanecer suspenso.") from None
            if not caminho.lower().endswith(".xml"):
                raise ErroLeitura("Inventário remoto contém arquivo inesperado.")
            for chave in CHAVE.findall(PurePosixPath(caminho).stem):
                caminhos = inventario.setdefault(chave, [])
                if caminho not in caminhos:
                    caminhos.append(caminho)
        return inventario

    def enviar(self, caminho_local, destino_relativo, *, sha256_esperado=None):
        # A barreira deve vir antes até da validação de instalação/arquivos.
        if not self.permitir_envio:
            raise EnvioDesabilitado("Envio real desabilitado; nenhum comando de escrita foi iniciado.")
        destino = validar_caminho_relativo(destino_relativo)
        nome = PurePosixPath(destino).name
        correspondencia = ARQUIVO_XML.fullmatch(nome)
        if not correspondencia:
            raise ErroTransporte("Nome de entrega deve seguir CHAVE-nfe.xml.")
        if len(PurePosixPath(destino).parts) < 2:
            raise ErroTransporte("A entrega precisa indicar uma pasta de canal.")
        self._validar_instalacao(ErroTransporte)
        try:
            conteudo = Path(caminho_local).read_bytes()
        except (OSError, TypeError, ValueError):
            raise ErroTransporte("XML local não está disponível para envio.") from None
        if sha256_esperado is not None:
            if not isinstance(sha256_esperado, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha256_esperado):
                raise ErroTransporte("SHA-256 esperado inválido; envio não iniciado.")
            if hashlib.sha256(conteudo).hexdigest() != sha256_esperado.lower():
                raise ErroTransporte("Conteúdo local diverge do registro de entrega; revisão necessária.")
        validar_xml_completo(conteudo, correspondencia.group("chave"))
        # Congela os bytes validados; o arquivo de origem pode ser reorganizado
        # por outro componente enquanto o rclone trabalha.
        with tempfile.TemporaryDirectory(prefix="bling-envio-xml-") as pasta:
            arquivo = Path(pasta) / nome
            arquivo.write_bytes(conteudo)
            try:
                resultado = self._executar([
                    "copyto", str(arquivo), self.remote + destino,
                    "--immutable", "--transfers", "1",
                    "--retries", "1", "--low-level-retries", "1",
                    "--dropbox-batch-mode", "off",
                ])
            except subprocess.TimeoutExpired:
                raise EntregaIncerta("Timeout no envio; conciliar a chave antes de qualquer nova tentativa.") from None
            except OSError:
                raise ErroTransporte("Não foi possível iniciar o processo rclone.") from None
            except UnicodeError:
                raise EntregaIncerta("Resultado do envio ilegível; conciliar a chave antes de qualquer nova tentativa.") from None
            if resultado.returncode != 0:
                raise EntregaIncerta("O envio não confirmou sucesso; conciliar a chave antes de qualquer nova tentativa.")
