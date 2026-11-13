"""Gera o pacote de preparação e emissão GNRE no Debian."""

import argparse
from pathlib import Path
import tempfile
import zipfile


BASE_DIR = Path(__file__).resolve().parent
SCHEMAS = (
    "lote_gnre_v2.00.xsd", "dados_gnre_v1.00.xsd", "dados_gnre_v2.00.xsd",
    "tiposBasicoGNRE_v1.00.xsd", "tiposBasicoGNRE_v2.00.xsd",
    "lote_gnre_consulta_v1.00.xsd", "lote_gnre_recibo_v1.00.xsd", "lote_gnre_result_v2.00.xsd",
)
ARQUIVOS = (
    "Dockerfile.gnre", "docker-compose.gnre.yml", "consultar_gnre.py", "preparar_gnre.py", "emitir_gnre.py",
    "scripts/testar_gnre_debian.sh", "scripts/executar_gnre_debian.sh",
    *("schemas/gnre/" + nome for nome in SCHEMAS),
)


def gerar_pacote(destino=None, xml_amostra=None, raiz=BASE_DIR):
    raiz = Path(raiz)
    destino = Path(destino or raiz / "dist/GNRE-Homologacao-Debian.zip")
    nomes = (*ARQUIVOS, "organizar_xmls.py", ".dockerignore")
    conteudos = {nome: (raiz / nome).read_bytes() for nome in nomes}
    wheels = list((raiz / "dependencias_gnre").glob("lxml-6.1.3-cp314-cp314-*x86_64.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Inclua o wheel lxml 6.1.3 para Python 3.14 Linux amd64 em dependencias_gnre.")
    for wheel in wheels:
        conteudos["dependencias_gnre/" + wheel.name] = wheel.read_bytes()
    if xml_amostra:
        from preparar_gnre import ler_nfe
        ler_nfe(xml_amostra)
        conteudos["entrada/nota-homologacao.xml"] = Path(xml_amostra).read_bytes()
    conteudos["LEIA-ME.txt"] = """GNRE NO DEBIAN

Requer o certificado instalado e a imagem automacao-xml-bling:2.0-dropbox.
Execute como e-c, com Python 3, Docker e Compose.
Extraia em pasta dedicada. Preserve dados-gnre ao atualizar.

Para validar o fluxo em homologacao, use datas explicitas no formato AAAA-MM-DD
bash scripts/executar_gnre_debian.sh executar --xml /entrada/nota-homologacao.xml --vencimento AAAA-MM-DD --pagamento AAAA-MM-DD

O padrao e homologacao. A preparacao consulta as regras da UF e valida o XML
pelos XSDs GNRE. O fluxo inicial atende DIFAL e FCP do DIFAL, por operacao.
O produto preferido e Materiais de Construcao, Acabamentos, Bricolagens ou
Adornos; a alternativa e Material Eletrico, conforme a configuracao anterior.
Parametros obrigatorios sem mapeamento interrompem a preparacao.

As datas da guia sao explicitas. Os valores principais vem do XML da NF-e.
Uma guia e preparada para cada imposto positivo. Cada lote pertence a um CNPJ.

O acompanhamento salva recibo, XMLs, resumo e PDF do lote em dados-gnre.
Retorno 2 indica processamento pendente. Retome com
bash scripts/executar_gnre_debian.sh consultar --lote-id IDENTIFICADOR

O recibo permite continuar consultando sem transmitir novamente o lote.
Se o envio terminar com resultado indeterminado, confira a recepcao antes
de tentar uma nova transmissao. Preserve o controle.sqlite3.

A emissao em producao exige --ambiente producao --confirmar-producao antes
do comando executar ou enviar. Valide os parametros da guia antes dessa etapa.

Os XSDs foram obtidos em 12/11/2026 do repositorio publico ACBr
https://svn.code.sf.net/p/acbr/code/trunk2/Exemplos/ACBrDFe/Schemas/GNRe/
""".encode("utf-8")
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pacote-gnre-", dir=destino.parent) as pasta:
        temporario = Path(pasta) / destino.name
        with zipfile.ZipFile(temporario, "w", zipfile.ZIP_DEFLATED) as pacote:
            for nome, conteudo in conteudos.items():
                if nome.endswith(".sh"):
                    conteudo = conteudo.replace(b"\r\n", b"\n")
                pacote.writestr(nome, conteudo)
        with zipfile.ZipFile(temporario) as pacote:
            if pacote.testzip() is not None:
                raise RuntimeError("Pacote GNRE com falha de integridade.")
        temporario.replace(destino)
    return destino.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml-amostra", type=Path)
    parser.add_argument("--destino", type=Path)
    args = parser.parse_args()
    print(gerar_pacote(args.destino, args.xml_amostra))


if __name__ == "__main__":
    main()
