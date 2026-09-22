import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("AUTOMACAO_ENV_FILE", BASE_DIR / ".env"))
load_dotenv(ENV_FILE)

CLIENT_ID = os.getenv("BLING_CLIENT_ID")
CLIENT_SECRET = os.getenv("BLING_CLIENT_SECRET")

TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"
TOKENS_FILE = Path(os.getenv("BLING_TOKENS_FILE", BASE_DIR / "tokens.json"))


def carregar_tokens():
    if not TOKENS_FILE.exists():
        raise RuntimeError(
            "tokens.json não encontrado. Execute autenticar_bling.py primeiro."
        )

    with TOKENS_FILE.open("r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def salvar_tokens(tokens):
    dados = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 21600),
        "scope": tokens.get("scope"),
        "obtained_at": int(time.time()),
    }

    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporario = TOKENS_FILE.with_suffix(".tmp")

    with temporario.open("w", encoding="utf-8") as arquivo:
        json.dump(dados, arquivo, indent=4)

    temporario.replace(TOKENS_FILE)


def token_precisa_renovar(tokens):
    obtained_at = tokens.get("obtained_at")
    expires_in = tokens.get("expires_in")

    if not obtained_at or not expires_in:
        return True

    expira_em = obtained_at + expires_in

    margem_seguranca = 300

    return time.time() >= (expira_em - margem_seguranca)


def renovar_access_token(refresh_token):
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "BLING_CLIENT_ID ou BLING_CLIENT_SECRET não encontrados no .env."
        )

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "enable-jwt": "1",
    }

    dados = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    resposta = requests.post(
        TOKEN_URL,
        headers=headers,
        data=dados,
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )

    if not resposta.ok:
        raise RuntimeError(
            f"Não foi possível renovar o token. "
            f"HTTP {resposta.status_code}: {resposta.text}"
        )

    novos_tokens = resposta.json()

    salvar_tokens(novos_tokens)

    print("Access token renovado automaticamente.")

    return novos_tokens


def obter_access_token():
    tokens = carregar_tokens()

    if token_precisa_renovar(tokens):
        tokens = renovar_access_token(tokens["refresh_token"])

    return tokens["access_token"]


def criar_headers():
    access_token = obter_access_token()

    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "enable-jwt": "1",
    }
