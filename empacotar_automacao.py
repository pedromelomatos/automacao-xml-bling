"""Gera o ZIP da automação diária a partir de uma lista explícita de arquivos."""

import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
NOME_PACOTE = "Pacote-Automacao-XML-Bling.zip"
HASH_PYTHON = "9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649"
CODIGOS = (
    "baixar_xmls.py", "organizar_xmls.py", "bling_auth.py", "autenticar_bling.py",
    "executar_diario.py", "agendador_docker.py", "preparar_instalacao.py",
    "requirements.txt", "configuracao.json", "configuracao.example.json", ".env.example",
)

LEIA_ME = """AUTOMAÇÃO DIÁRIA XML BLING - PACOTE ATUALIZADO
=============================================

Extraia todo o ZIP para uma pasta fixa. Use somente uma opção de instalação.

Antes de instalar, o TI deve colocar .env e tokens.json em arquivos-automacao.
Essas credenciais não estão incluídas no pacote. A configuração das unidades,
marketplaces e UFs está em arquivos-automacao/configuracao.json; confira os dados.

WINDOWS: abra instalacao-windows/Instalar automacao.cmd e escolha a primeira janela.
DOCKER: siga instalacao-docker/LEIA-ME.txt para escolher a primeira janela.

A instalação testa a conexão e a permissão de consulta de NF-e no Bling antes de
ativar a automação. O teste não baixa XMLs e pode renovar os tokens de autorização.
Em caso de falha, corrija a conexão ou as credenciais e repita a instalação.

A execução diária ocorre às 14h10, processando a janela de ontem às 14h até hoje
às 14h. No Windows, confira o fuso de Brasília e mantenha o usuário conectado;
a tela pode ficar bloqueada. O Docker usa America/Sao_Paulo.

Pastas:
- arquivos-automacao: código, configuração e credenciais adicionadas pelo TI;
- dados: XMLs, logs, primeira janela configurada e histórico de execuções;
- instalacao-windows: instalador e dependências offline para Windows;
- instalacao-docker: arquivos Docker e dependências Linux.

Este ZIP não contém estado_execucao.json nem configuracao_execucao.json de outra
máquina. A data escolhida é salva somente após a validação do Bling e não marca
nenhuma janela como concluída. Reinstalações preservam a escolha e o progresso.

Ao migrar uma instalação existente, pare a execução anterior e transfira também
o conteúdo de dados, .env e tokens.json por um meio seguro. Assim, a nova máquina
continua do último sucesso. Mantenha somente uma instalação ativa.

O robô organiza as NF-e e as cópias GNRE. A rotina diária não gera ZIPs de GNRE.
"""

LEIA_WINDOWS = """INSTALAÇÃO WINDOWS
==================

1. Coloque .env e tokens.json na pasta arquivos-automacao e confira configuracao.json.
2. Abra "Instalar automacao.cmd" nesta pasta.
3. Na primeira instalação, escolha:
   1 - Começar pela última janela encerrada;
   2 - Recuperar desde uma data (DD/MM/AAAA ou AAAA-MM-DD).
   A data identifica a janela encerrada às 14h desse dia; ela começa no dia anterior.
4. O instalador verifica os arquivos, instala o Python e as bibliotecas offline
   quando necessário, testa o acesso ao Bling, simula as pendências e só então
   cria a tarefa diária das 14h10. Internet é necessária para validar o Bling.
5. Mantenha o usuário do Windows conectado e o computador no fuso de Brasília.
   A tela pode ficar bloqueada.

Reinstalações preservam a escolha e o histórico. Os dados ficam em ../dados.
Uma falha de validação não cria nem altera a tarefa agendada.

Validação sem criar tarefa ou salvar a escolha (pode renovar tokens):
    powershell -NoProfile -ExecutionPolicy Bypass -File instalar_automacao.ps1 -ValidarSomente

Instalação sem perguntas, escolhendo a primeira janela:
    powershell -NoProfile -ExecutionPolicy Bypass -File instalar_automacao.ps1 -PrimeiraData AAAA-MM-DD
Ou, para começar pela última janela encerrada:
    powershell -NoProfile -ExecutionPolicy Bypass -File instalar_automacao.ps1 -UltimaJanela
"""

LEIA_DOCKER = """INSTALAÇÃO DOCKER
=================

Requer Docker Engine/Desktop com Compose, arquitetura amd64 e internet.

1. Coloque .env e tokens.json na pasta arquivos-automacao e confira configuracao.json.
2. Escolha a primeira janela antes de iniciar:
   - Para começar pela última janela encerrada, não acrescente nenhuma variável.
   - Para recuperar desde uma data, acrescente ao .env:
         AUTOMACAO_PRIMEIRA_DATA=AAAA-MM-DD
     Substitua AAAA-MM-DD pela data da primeira janela, já encerrada às 14h.
     A janela começa às 14h do dia anterior à data escolhida.
3. Abra um terminal nesta pasta e execute:
       docker compose up -d --build
4. Confira a validação do Bling e o funcionamento:
       docker compose logs -f automacao-xml-bling

O acesso ao Bling é validado antes de iniciar downloads e agendamento. Se falhar,
o processo termina sem avançar o histórico ou salvar a escolha; o Docker pode
reiniciá-lo conforme sua política. Corrija a causa e reinicie o serviço.
O teste pode renovar os tokens, mas não baixa XMLs.

A escolha fica em ../dados/configuracao_execucao.json. Reinícios e reinstalações
preservam a escolha e o histórico; alterações posteriores da variável .env não
mudam a data de uma instalação já configurada. Execução diária às 14h10 no fuso
America/Sao_Paulo, com recuperação de pendências e retentativas em caso de falha.

Para somente validar antes de subir o serviço (pode renovar tokens):
    docker compose run --rm automacao-xml-bling python preparar_instalacao.py --validar-somente

Parar:      docker compose down
Reiniciar:  docker compose restart
Use somente uma instalação ativa por vez.
"""


def gerar_pacote(raiz=BASE_DIR):
    raiz = Path(raiz).resolve()
    instalador = raiz / "python-3.14.7-amd64.exe"
    obrigatorios = [*(raiz / nome for nome in CODIGOS), instalador,
                    raiz / "instalar_automacao.ps1", raiz / "Instalar automacao.cmd",
                    raiz / "Dockerfile", raiz / "docker-compose.yml", raiz / ".dockerignore"]
    ausentes = [p.name for p in obrigatorios if not p.is_file()]
    if ausentes:
        raise RuntimeError("Arquivos ausentes: " + ", ".join(ausentes))
    if hashlib.sha256(instalador.read_bytes()).hexdigest() != HASH_PYTHON:
        raise RuntimeError("O instalador do Python não passou na verificação SHA256.")
    for pasta in ("dependencias", "dependencias_docker"):
        if not list((raiz / pasta).glob("*.whl")):
            raise RuntimeError(f"Dependências offline ausentes: {pasta}.")

    pasta_tmp = raiz / "tmp"
    pasta_tmp.mkdir(exist_ok=True)
    destino = raiz / NOME_PACOTE
    with tempfile.TemporaryDirectory(prefix="entrega-diaria-", dir=pasta_tmp) as temporaria:
        pacote = Path(temporaria) / "pacote"
        codigo = pacote / "arquivos-automacao"
        windows = pacote / "instalacao-windows"
        docker = pacote / "instalacao-docker"
        for pasta in (codigo, windows, docker, pacote / "dados"):
            pasta.mkdir(parents=True)
        for nome in CODIGOS:
            shutil.copy2(raiz / nome, codigo / nome)
        for nome in ("instalar_automacao.ps1", "Instalar automacao.cmd", instalador.name):
            shutil.copy2(raiz / nome, windows / nome)
        for origem, destino_dependencias in (
            (raiz / "dependencias", windows / "dependencias"),
            (raiz / "dependencias_docker", docker / "dependencias"),
        ):
            destino_dependencias.mkdir()
            for p in origem.glob("*.whl"):
                shutil.copy2(p, destino_dependencias / p.name)

        dockerfile = (raiz / "Dockerfile").read_text(encoding="utf-8")
        dockerfile = dockerfile.replace("/segredos", "/credenciais")
        dockerfile = dockerfile.replace("COPY requirements.txt", "COPY arquivos-automacao/requirements.txt")
        dockerfile = dockerfile.replace("COPY dependencias_docker/", "COPY instalacao-docker/dependencias/")
        fontes = "baixar_xmls.py bling_auth.py organizar_xmls.py executar_diario.py agendador_docker.py preparar_instalacao.py"
        dockerfile = dockerfile.replace("COPY " + fontes, "COPY " + " ".join("arquivos-automacao/" + n for n in fontes.split()))
        dockerfile = dockerfile.replace("COPY configuracao.json", "COPY arquivos-automacao/configuracao.json")
        (docker / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        compose = (raiz / "docker-compose.yml").read_text(encoding="utf-8")
        compose = compose.replace("context: .", "context: ..").replace("dockerfile: Dockerfile", "dockerfile: instalacao-docker/Dockerfile")
        compose = compose.replace("- .env", "- ../arquivos-automacao/.env")
        compose = compose.replace("BLING_TOKENS_FILE: /segredos/tokens.json", "BLING_TOKENS_FILE: /credenciais/tokens.json")
        compose = compose.replace("./dados:/dados", "../dados:/dados")
        compose = compose.replace("./segredos:/segredos", "../arquivos-automacao:/credenciais")
        compose = compose.replace("./configuracao.json:", "../arquivos-automacao/configuracao.json:")
        (docker / "docker-compose.yml").write_text(compose, encoding="utf-8")
        ignorados = ["*", "!arquivos-automacao/"]
        ignorados.extend("!arquivos-automacao/" + nome for nome in CODIGOS if nome not in ("autenticar_bling.py", ".env.example", "configuracao.example.json"))
        ignorados += ["!instalacao-docker/", "!instalacao-docker/dependencias/", "!instalacao-docker/dependencias/*.whl"]
        (docker / "Dockerfile.dockerignore").write_text("\n".join(ignorados) + "\n", encoding="utf-8")

        for caminho, texto in (
            (pacote / "LEIA-ME.txt", LEIA_ME), (windows / "LEIA-ME.txt", LEIA_WINDOWS),
            (docker / "LEIA-ME.txt", LEIA_DOCKER),
            (codigo / "COLOQUE AS CREDENCIAIS AQUI.txt", "Coloque .env e tokens.json nesta pasta antes da instalação.\nTransfira as credenciais por um meio seguro.\n"),
            (pacote / "dados" / "LEIA-ME.txt", "Esta pasta começa sem histórico. A instalação salvará aqui a primeira janela escolhida.\n"),
        ):
            caminho.write_text(texto, encoding="utf-8")

        arquivos = sorted(p for p in pacote.rglob("*") if p.is_file())
        manifesto = {
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "arquivos_sha256": {
                p.relative_to(pacote).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in arquivos
            },
        }
        (pacote / "manifesto.json").write_text(json.dumps(manifesto, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        zip_temporario = Path(temporaria) / NOME_PACOTE
        with zipfile.ZipFile(zip_temporario, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as arquivo_zip:
            for p in sorted(pacote.rglob("*")):
                if p.is_file():
                    arquivo_zip.write(p, p.relative_to(pacote).as_posix())
        with zipfile.ZipFile(zip_temporario) as arquivo_zip:
            if arquivo_zip.testzip() is not None:
                raise RuntimeError("Falha de integridade no ZIP gerado.")
        zip_temporario.replace(destino)
    return destino


if __name__ == "__main__":
    try:
        print(f"Pacote atualizado: {gerar_pacote()}")
    except (OSError, RuntimeError) as erro:
        raise SystemExit(f"Falha ao gerar pacote: {erro}")
