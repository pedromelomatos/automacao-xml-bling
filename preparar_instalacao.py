"""Escolhe a primeira janela e valida o Bling antes de ativar a automação."""

import argparse
import json
import os
from datetime import date, datetime, time, timedelta

import requests

import bling_auth
from baixar_xmls import ClienteBling
from executar_diario import (
    ARQUIVO_CONFIGURACAO_EXECUCAO,
    DATA_DIR,
    bloqueio_exclusivo,
    carregar_estado,
    carregar_primeira_data,
    ultima_janela_encerrada,
)
from organizar_xmls import carregar_configuracao


def validar_conexao_bling():
    """Consulta uma página mínima de NF-e; não baixa XMLs nem muda o estado."""
    if not bling_auth.CLIENT_ID or not bling_auth.CLIENT_SECRET:
        raise RuntimeError("Preencha BLING_CLIENT_ID e BLING_CLIENT_SECRET no .env.")
    try:
        tokens = bling_auth.carregar_tokens()
        if not isinstance(tokens, dict) or not all(
            isinstance(tokens.get(campo), str) and tokens[campo].strip()
            for campo in ("access_token", "refresh_token")
        ):
            raise ValueError("Tokens incompletos")
        access_token = bling_auth.obter_access_token()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, requests.RequestException):
        # A resposta de renovação pode conter credenciais; não a exibe no instalador.
        raise RuntimeError(
            "Não foi possível carregar ou renovar a autorização do Bling. "
            "Confira .env e tokens.json; autorize novamente se necessário."
        ) from None

    cliente = ClienteBling(access_token)
    try:
        limite = ultima_janela_encerrada(datetime.now())
        resposta = cliente.get(
            "/nfe",
            params={
                "pagina": 1,
                "limite": 1,
                "tipo": 1,
                "dataEmissaoInicial": datetime.combine(
                    limite - timedelta(days=1), time(14)
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "dataEmissaoFinal": datetime.combine(limite, time(14)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            },
        )
        dados = resposta.json()
        if not isinstance(dados, dict) or not isinstance(dados.get("data"), list):
            raise ValueError("Resposta inesperada")
    except RuntimeError as erro:
        if "HTTP 401" in str(erro) or "HTTP 403" in str(erro):
            mensagem = "O Bling recusou o acesso às NF-e. Confira a autorização e as permissões."
        else:
            mensagem = "A consulta de teste ao Bling falhou. Confira a internet e tente novamente."
        raise RuntimeError(mensagem) from None
    except (ValueError, requests.RequestException):
        raise RuntimeError("O Bling não retornou uma lista válida de NF-e na consulta de teste.") from None
    finally:
        cliente.close()


def validar_configuracao_local():
    configuracao = carregar_configuracao()
    if not configuracao["unidades"]:
        raise RuntimeError("Configure as unidades emitentes em configuracao.json.")
    for cnpj, unidade in configuracao["unidades"].items():
        if len(cnpj) != 14 or not cnpj.isdecimal() or not isinstance(unidade, str) or not unidade.strip():
            raise RuntimeError("Há uma unidade inválida em configuracao.json.")
        ufs = configuracao["gnre_ufs_por_unidade"].get(unidade)
        if not isinstance(ufs, list):
            raise RuntimeError("Configure as regras de GNRE de todas as unidades em configuracao.json.")


def escolher_primeira_data(limite, interativo=False, primeira_data=None, ultima_janela=False):
    if primeira_data is None and not ultima_janela:
        texto_env = os.getenv("AUTOMACAO_PRIMEIRA_DATA", "").strip()
        if texto_env:
            try:
                primeira_data = date.fromisoformat(texto_env)
            except ValueError:
                raise RuntimeError("AUTOMACAO_PRIMEIRA_DATA deve usar AAAA-MM-DD.") from None
        elif interativo:
            while True:
                print(f"1 - Começar pela última janela encerrada ({limite:%d/%m/%Y})")
                print("2 - Recuperar desde uma data escolhida")
                opcao = input("Escolha [1]: ").strip() or "1"
                if opcao == "1":
                    break
                if opcao == "2":
                    texto = input("Primeira janela (DD/MM/AAAA ou AAAA-MM-DD): ").strip()
                    try:
                        primeira_data = (
                            datetime.strptime(texto, "%d/%m/%Y").date()
                            if "/" in texto else date.fromisoformat(texto)
                        )
                        if primeira_data > limite:
                            raise ValueError("Janela ainda aberta")
                        break
                    except ValueError:
                        print(f"Data inválida. Informe uma janela até {limite:%d/%m/%Y}.")
                else:
                    print("Escolha 1 ou 2.")
    primeira_data = primeira_data or limite
    if primeira_data > limite:
        raise RuntimeError(f"A primeira janela deve estar encerrada (até {limite:%d/%m/%Y}).")
    return primeira_data


def preparar_instalacao(
    interativo=False, primeira_data=None, ultima_janela=False, validar_somente=False,
):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with bloqueio_exclusivo():
        ultima_concluida = carregar_estado()
        if ultima_concluida is not None:
            if primeira_data is not None or ultima_janela:
                raise RuntimeError("Já existe histórico de execução. A reinstalação deve preservar o progresso.")
            print(f"Histórico preservado. Última janela concluída: {ultima_concluida:%d/%m/%Y}.")
            data_inicial = None
        else:
            data_inicial = carregar_primeira_data()
            if data_inicial is not None:
                if primeira_data is not None or ultima_janela:
                    raise RuntimeError("A primeira janela já foi configurada; ela será preservada.")
                print(f"Primeira janela já configurada: {data_inicial:%d/%m/%Y}.")
            elif not validar_somente:
                data_inicial = escolher_primeira_data(
                    ultima_janela_encerrada(datetime.now()), interativo,
                    primeira_data, ultima_janela,
                )

        validar_configuracao_local()
        print("Validando o acesso às NF-e no Bling...", flush=True)
        validar_conexao_bling()
        print("Conexão e permissão de consulta de NF-e confirmadas.", flush=True)
        if not validar_somente and ultima_concluida is None and not ARQUIVO_CONFIGURACAO_EXECUCAO.exists():
            dados = {
                "primeira_data": data_inicial.isoformat(),
                "configurado_em": datetime.now().isoformat(timespec="seconds"),
            }
            temporario = ARQUIVO_CONFIGURACAO_EXECUCAO.with_suffix(".json.tmp")
            try:
                temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporario.replace(ARQUIVO_CONFIGURACAO_EXECUCAO)
            finally:
                temporario.unlink(missing_ok=True)
            print(f"Primeira janela definida: {data_inicial:%d/%m/%Y}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    escolha = parser.add_mutually_exclusive_group()
    escolha.add_argument("--primeira-data", type=date.fromisoformat)
    escolha.add_argument("--ultima-janela", action="store_true")
    parser.add_argument("--interativo", action="store_true")
    parser.add_argument("--validar-somente", action="store_true")
    argumentos = parser.parse_args()
    try:
        preparar_instalacao(**vars(argumentos))
    except (OSError, RuntimeError, EOFError, KeyboardInterrupt) as erro:
        mensagem = str(erro) if isinstance(erro, RuntimeError) else "Instalação interrompida; confira os arquivos e tente novamente."
        print(f"[ERRO] {mensagem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
