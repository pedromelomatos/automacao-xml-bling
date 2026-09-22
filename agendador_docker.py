"""Mantém o executor diário ativo dentro do contêiner Docker."""

import subprocess
import sys
import time
from datetime import datetime, time as horario, timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
EXECUTOR = BASE_DIR / "executar_diario.py"
HORARIO_DIARIO = horario(14, 10)
MAX_TENTATIVAS = 3
INTERVALO_TENTATIVAS = 10 * 60


def proxima_execucao(agora):
    proxima = datetime.combine(agora.date(), HORARIO_DIARIO)
    if proxima <= agora:
        proxima += timedelta(days=1)
    return proxima


def executar_com_retentativas(executar=None, esperar=time.sleep):
    executar = executar or executar_executor
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        print(
            f"Iniciando executor diário (tentativa {tentativa}/{MAX_TENTATIVAS}).",
            flush=True,
        )
        codigo = executar()
        if codigo == 0:
            print("Executor diário concluído com sucesso.", flush=True)
            return True
        if tentativa < MAX_TENTATIVAS:
            print(
                f"Executor terminou com código {codigo}; nova tentativa em 10 minutos.",
                flush=True,
            )
            esperar(INTERVALO_TENTATIVAS)
    print("Executor diário falhou após três tentativas.", flush=True)
    return False


def executar_executor():
    resultado = subprocess.run(
        [sys.executable, str(EXECUTOR)],
        cwd=BASE_DIR,
        check=False,
    )
    return resultado.returncode


def esperar_ate(instante):
    while True:
        segundos = (instante - datetime.now()).total_seconds()
        if segundos <= 0:
            return
        time.sleep(min(segundos, 300))


def main():
    print("Agendador Docker iniciado. Horário diário: 14h10.", flush=True)
    executar_com_retentativas()
    while True:
        proxima = proxima_execucao(datetime.now())
        print(
            f"Próxima execução: {proxima.strftime('%d-%m-%Y %H:%M:%S')}.",
            flush=True,
        )
        esperar_ate(proxima)
        executar_com_retentativas()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Agendador Docker encerrado.", flush=True)
