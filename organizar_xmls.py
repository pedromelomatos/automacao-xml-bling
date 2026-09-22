"""Organiza XMLs de NF-e por CNPJ, marketplace, data e UF de destino."""

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree


BASE_DIR = Path(__file__).resolve().parent


def obter_pasta_dados():
    configurada = os.getenv("AUTOMACAO_DATA_DIR")
    if configurada:
        return Path(configurada).expanduser().resolve()
    compartilhada = BASE_DIR.parent / "dados"
    if compartilhada.is_dir():
        return compartilhada.resolve()
    return BASE_DIR


DATA_DIR = obter_pasta_dados()
CONFIG_FILE = Path(os.getenv("AUTOMACAO_CONFIG_FILE", BASE_DIR / "configuracao.json"))
PASTA_ORGANIZADOS = DATA_DIR / "xml_por_cnpj"


@dataclass(frozen=True)
class ResultadoOrganizacao:
    unidade: str
    marketplace: str
    data_emissao: str
    uf_destino: str
    destinos: tuple[Path]
    novos: int
    existentes: int
    aviso: str | None = None


def carregar_configuracao(caminho=CONFIG_FILE):
    try:
        with caminho.open("r", encoding="utf-8") as arquivo:
            configuracao = json.load(arquivo)
    except (OSError, json.JSONDecodeError) as erro:
        raise RuntimeError(f"Não foi possível ler {caminho.name}.") from erro

    for campo in ("unidades", "canais", "intermediadores", "gnre_ufs_por_unidade"):
        if not isinstance(configuracao.get(campo), dict):
            raise RuntimeError(f"Campo '{campo}' inválido em {caminho.name}.")
    return configuracao


def nome_local(elemento):
    return elemento.tag.rsplit("}", 1)[-1]


def primeiro_texto(raiz, caminho):
    atuais = [raiz]
    for parte in caminho:
        proximos = []
        for atual in atuais:
            proximos.extend(
                filho for filho in atual if nome_local(filho) == parte
            )
        atuais = proximos
        if not atuais:
            return None
    texto = atuais[0].text
    return texto.strip() if texto else None


def primeiro_descendente(raiz, nome):
    for elemento in raiz.iter():
        if nome_local(elemento) == nome:
            texto = elemento.text
            return texto.strip() if texto else None
    return None


def limpar_componente_pasta(valor):
    valor = unicodedata.normalize("NFKC", str(valor)).strip()
    valor = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", valor)
    valor = re.sub(r"\s+", " ", valor).rstrip(". ")
    if not valor:
        return "_SEM_NOME"
    if valor.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        return f"_{valor}"
    return valor[:100]


def obter_metadados_xml(caminho_xml):
    try:
        raiz = ElementTree.parse(caminho_xml).getroot()
    except (OSError, ElementTree.ParseError) as erro:
        raise RuntimeError(f"XML inválido: {caminho_xml.name}.") from erro

    inf_nfe = next(
        (elemento for elemento in raiz.iter() if nome_local(elemento) == "infNFe"),
        None,
    )
    if inf_nfe is None:
        raise RuntimeError(f"NF-e não encontrada em {caminho_xml.name}.")

    cnpj_emitente = primeiro_texto(inf_nfe, ("emit", "CNPJ"))
    data_texto = primeiro_texto(inf_nfe, ("ide", "dhEmi"))
    if not data_texto:
        data_texto = primeiro_texto(inf_nfe, ("ide", "dEmi"))
    uf_destino = primeiro_texto(inf_nfe, ("dest", "enderDest", "UF"))
    cnpj_intermediador = primeiro_texto(inf_nfe, ("infIntermed", "CNPJ"))
    chave = primeiro_descendente(raiz, "chNFe")
    if not chave:
        identificador = inf_nfe.attrib.get("Id", "")
        chave = identificador[3:] if identificador.startswith("NFe") else caminho_xml.stem

    if not cnpj_emitente or len(cnpj_emitente) != 14:
        raise RuntimeError(f"CNPJ emitente ausente em {caminho_xml.name}.")
    try:
        data_emissao = datetime.fromisoformat(data_texto).date().strftime("%d-%m-%Y")
    except (TypeError, ValueError) as erro:
        raise RuntimeError(f"Data de emissão inválida em {caminho_xml.name}.") from erro
    if not uf_destino:
        uf_destino = "SEM_UF"
    if len(chave) != 44 or not chave.isdecimal():
        raise RuntimeError(f"Chave de acesso inválida em {caminho_xml.name}.")

    return {
        "cnpj_emitente": cnpj_emitente,
        "data_emissao": data_emissao,
        "uf_destino": uf_destino.upper(),
        "cnpj_intermediador": cnpj_intermediador,
        "chave": chave,
    }


def obter_marketplace(nota, metadados, configuracao):
    id_canal = None
    if nota:
        loja = nota.get("loja") or {}
        id_canal = loja.get("id")
    if id_canal is not None:
        marketplace = configuracao["canais"].get(str(id_canal))
        if marketplace:
            return marketplace, None

    cnpj_intermediador = metadados.get("cnpj_intermediador")
    if cnpj_intermediador:
        marketplace = configuracao["intermediadores"].get(cnpj_intermediador)
        if marketplace:
            aviso = None
            if id_canal is not None:
                aviso = f"canal {id_canal} identificado pelo intermediador"
            return marketplace, aviso

    if id_canal is not None:
        return f"Canal {id_canal}", f"canal {id_canal} ainda não está nomeado"
    return "Sem marketplace", "NF-e sem canal ou intermediador identificado"


def copiar_sem_sobrescrever(origem, destino):
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        if origem.read_bytes() != destino.read_bytes():
            raise RuntimeError(f"Já existe um XML diferente em {destino}.")
        return False

    temporario = destino.with_name(f"{destino.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(origem, temporario)
        temporario.replace(destino)
    finally:
        temporario.unlink(missing_ok=True)
    return True


def organizar_xml(
    caminho_xml,
    nota=None,
    pasta_saida=PASTA_ORGANIZADOS,
    configuracao=None,
    incluir_gnre=True,
):
    caminho_xml = Path(caminho_xml)
    configuracao = configuracao or carregar_configuracao()
    metadados = obter_metadados_xml(caminho_xml)

    unidade = configuracao["unidades"].get(metadados["cnpj_emitente"])
    if not unidade:
        raise RuntimeError(
            f"CNPJ emitente não mapeado em {CONFIG_FILE.name}: "
            f"{metadados['cnpj_emitente']}."
        )
    marketplace, aviso = obter_marketplace(nota, metadados, configuracao)

    unidade_pasta = limpar_componente_pasta(unidade)
    marketplace_pasta = limpar_componente_pasta(marketplace)
    uf_pasta = limpar_componente_pasta(metadados["uf_destino"])
    nome_arquivo = f"{metadados['chave']}.xml"

    destino_principal = (
        Path(pasta_saida)
        / unidade_pasta
        / marketplace_pasta
        / metadados["data_emissao"]
        / uf_pasta
        / nome_arquivo
    )

    
    destinos = [destino_principal]

    if incluir_gnre:
        ufs_gnre_configuradas = configuracao["gnre_ufs_por_unidade"].get(unidade)
        if not isinstance(ufs_gnre_configuradas, list):
            raise RuntimeError(
                f"Regras de GNRE ausentes para a unidade '{unidade}' em "
                f"{CONFIG_FILE.name}."
            )

        ufs_gnre = {
            str(uf).strip().upper()
            for uf in ufs_gnre_configuradas
            if str(uf).strip()
        }

        if metadados["uf_destino"] in ufs_gnre:
            destino_gnre = (
                Path(pasta_saida)
                / unidade_pasta
                / marketplace_pasta
                / "GNRE"
                / metadados["data_emissao"]
                / uf_pasta
                / nome_arquivo
            )
            destinos.append(destino_gnre)

    destinos = tuple(destinos)

    # copia o XML para cada destino e conta só as novas cópias criadas
    novos = sum(copiar_sem_sobrescrever(caminho_xml, destino) for destino in destinos)
    return ResultadoOrganizacao(
        unidade=unidade,
        marketplace=marketplace,
        data_emissao=metadados["data_emissao"],
        uf_destino=metadados["uf_destino"],
        destinos=destinos,
        novos=novos,
        existentes=len(destinos) - novos,
        aviso=aviso,
    )


def organizar_pasta(pasta_origem, pasta_saida=PASTA_ORGANIZADOS):
    pasta_origem = Path(pasta_origem)
    arquivos = sorted(pasta_origem.glob("*.xml"))
    configuracao = carregar_configuracao()
    novos = existentes = erros = avisos = 0

    print(f"XMLs encontrados: {len(arquivos)}")
    for indice, caminho in enumerate(arquivos, start=1):
        try:
            resultado = organizar_xml(
                caminho,
                pasta_saida=pasta_saida,
                configuracao=configuracao,
            )
            novos += resultado.novos
            existentes += resultado.existentes
            if resultado.aviso:
                avisos += 1
                print(f"AVISO [{indice}/{len(arquivos)}] {resultado.aviso}.")
        except RuntimeError as erro:
            erros += 1
            print(f"ERRO [{indice}/{len(arquivos)}] {erro}")

    print(
        f"Organização finalizada | XMLs: {len(arquivos)} | "
        f"cópias criadas: {novos} | existentes: {existentes} | "
        f"avisos: {avisos} | erros: {erros}"
    )
    print(f"Pasta: {Path(pasta_saida).resolve()}")
    return 1 if erros else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pasta", type=Path, help="pasta que contém os XMLs")
    parser.add_argument(
        "--saida",
        type=Path,
        default=PASTA_ORGANIZADOS,
        help="pasta de saída (padrão: xml_por_cnpj)",
    )
    argumentos = parser.parse_args()
    if not argumentos.pasta.is_dir():
        parser.error(f"pasta não encontrada: {argumentos.pasta}")
    return organizar_pasta(argumentos.pasta, argumentos.saida)


if __name__ == "__main__":
    sys.exit(main())
