"""Executa as janelas diárias de NF-e e recupera dias pendentes."""

import argparse
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, time as horario, timedelta
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


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
ARQUIVO_ESTADO = DATA_DIR / "estado_execucao.json"
ARQUIVO_BLOQUEIO = DATA_DIR / ".executor_diario.lock"
SCRIPT_DOWNLOAD = BASE_DIR / "baixar_xmls.py"


def obter_argumentos():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simular",
        action="store_true",
        help="mostra as janelas pendentes sem executar downloads nem alterar o estado",
    )
    parser.add_argument(
        "--primeira-data",
        type=date.fromisoformat,
        help=(
            "primeira janela a processar no formato AAAA-MM-DD; "
            "usado somente quando ainda não existe estado"
        ),
    )
    return parser.parse_args()


def configurar_log():
    pasta_logs = DATA_DIR / "logs"
    pasta_logs.mkdir(parents=True, exist_ok=True)
    arquivo_log = pasta_logs / "executor_diario.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(arquivo_log, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    return arquivo_log


def ultima_janela_encerrada(agora):
    corte_de_hoje = datetime.combine(agora.date(), horario(14))
    if agora >= corte_de_hoje:
        return agora.date()
    return agora.date() - timedelta(days=1)


def carregar_estado(caminho=ARQUIVO_ESTADO):
    caminho = Path(caminho)
    if not caminho.exists():
        return None
    try:
        with caminho.open("r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
        return date.fromisoformat(dados["ultima_janela_concluida"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as erro:
        raise RuntimeError(f"Estado inválido em {caminho.name}.") from erro


def salvar_estado(data_concluida, caminho=ARQUIVO_ESTADO, agora=None):
    caminho = Path(caminho)
    agora = agora or datetime.now()
    dados = {
        "ultima_janela_concluida": data_concluida.isoformat(),
        "atualizado_em": agora.isoformat(timespec="seconds"),
    }
    temporario = caminho.with_name(f"{caminho.name}.{os.getpid()}.tmp")
    try:
        temporario.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporario.replace(caminho)
    finally:
        temporario.unlink(missing_ok=True)


def calcular_pendencias(ultima_concluida, limite, primeira_data=None):
    if ultima_concluida is None:
        inicio = primeira_data or limite
    else:
        if primeira_data is not None:
            raise RuntimeError(
                "--primeira-data só pode ser usado antes da criação do estado."
            )
        inicio = ultima_concluida + timedelta(days=1)

    if inicio > limite:
        return []
    quantidade = (limite - inicio).days + 1
    return [inicio + timedelta(days=indice) for indice in range(quantidade)]


def executar_data(data_execucao):
    resultado = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DOWNLOAD),
            data_execucao.isoformat(),
            "--janela-14h",
        ],
        cwd=BASE_DIR,
        check=False,
    )
    return resultado.returncode


def processar_pendencias(datas, executar=executar_data, registrar=salvar_estado):
    for indice, data_execucao in enumerate(datas, start=1):
        logging.info(
            "Processando janela %s (%s/%s).",
            data_execucao.strftime("%d-%m-%Y"),
            indice,
            len(datas),
        )
        codigo = executar(data_execucao)
        if codigo != 0:
            logging.error(
                "A janela %s falhou com código %s; o estado não será avançado.",
                data_execucao.strftime("%d-%m-%Y"),
                codigo,
            )
            return data_execucao
        registrar(data_execucao)
        logging.info(
            "Janela %s concluída e registrada.",
            data_execucao.strftime("%d-%m-%Y"),
        )
    return None


@contextmanager
def bloqueio_exclusivo(caminho=ARQUIVO_BLOQUEIO):
    caminho = Path(caminho)
    with caminho.open("a+b") as arquivo:
        arquivo.seek(0, os.SEEK_END)
        if arquivo.tell() == 0:
            arquivo.write(b"0")
            arquivo.flush()
        arquivo.seek(0)
        if os.name == "nt":
            try:
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as erro:
                raise RuntimeError("O executor diário já está em execução.") from erro
            try:
                yield
            finally:
                arquivo.seek(0)
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as erro:
                raise RuntimeError("O executor diário já está em execução.") from erro
            try:
                yield
            finally:
                fcntl.flock(arquivo.fileno(), fcntl.LOCK_UN)


def main():
    argumentos = obter_argumentos()
    os.chdir(BASE_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    arquivo_log = configurar_log()

    try:
        with bloqueio_exclusivo():
            limite = ultima_janela_encerrada(datetime.now())
            ultima_concluida = carregar_estado()
            pendencias = calcular_pendencias(
                ultima_concluida,
                limite,
                argumentos.primeira_data,
            )

            if ultima_concluida:
                logging.info(
                    "Última janela concluída: %s.",
                    ultima_concluida.strftime("%d-%m-%Y"),
                )
            else:
                logging.info("Primeira execução: ainda não existe estado salvo.")

            if not pendencias:
                logging.info("Nenhuma janela pendente. Limite atual: %s.", limite.strftime("%d-%m-%Y"))
                return 0

            logging.info(
                "Janelas pendentes: %s.",
                ", ".join(data.strftime("%d-%m-%Y") for data in pendencias),
            )
            if argumentos.simular:
                logging.info("Simulação concluída; nenhum download ou estado foi alterado.")
                return 0

            data_com_falha = processar_pendencias(pendencias)
            if data_com_falha:
                return 1
            logging.info("Todas as janelas pendentes foram concluídas. Log: %s", arquivo_log)
            return 0
    except (OSError, RuntimeError) as erro:
        logging.error("Executor diário interrompido: %s", erro)
        return 1


if __name__ == "__main__":
    sys.exit(main())
