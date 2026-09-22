import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv


load_dotenv()

CLIENT_ID = os.getenv("BLING_CLIENT_ID")
CLIENT_SECRET = os.getenv("BLING_CLIENT_SECRET")
REDIRECT_URI = os.getenv("BLING_REDIRECT_URI")

AUTH_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"
TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"

TOKENS_FILE = "tokens.json"


if not CLIENT_ID:
    raise RuntimeError("BLING_CLIENT_ID não encontrado no .env")

if not CLIENT_SECRET:
    raise RuntimeError("BLING_CLIENT_SECRET não encontrado no .env")

if not REDIRECT_URI:
    raise RuntimeError("BLING_REDIRECT_URI não encontrado no .env")


state_esperado = secrets.token_urlsafe(32)
resultado_callback = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        parametros = parse_qs(parsed.query)

        codigo = parametros.get("code", [None])[0]
        state_recebido = parametros.get("state", [None])[0]
        erro = parametros.get("error", [None])[0]

        if erro:
            resultado_callback["error"] = erro

            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()

            self.wfile.write(
                "<h2>Autorização não concluída.</h2>"
                "<p>Você pode fechar esta janela.</p>".encode("utf-8")
            )
            return

        if not codigo:
            resultado_callback["error"] = "Código de autorização não recebido."

            self.send_response(400)
            self.end_headers()
            return

        if state_recebido != state_esperado:
            resultado_callback["error"] = "State inválido."

            self.send_response(400)
            self.end_headers()
            return

        resultado_callback["code"] = codigo

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        self.wfile.write(
            "<h2>Autorização recebida com sucesso.</h2>"
            "<p>Você pode fechar esta janela e voltar ao terminal.</p>".encode(
                "utf-8"
            )
        )

    def log_message(self, format, *args):
        return


def obter_codigo_autorizacao():
    parametros = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "state": state_esperado,
    }

    url = f"{AUTH_URL}?{urlencode(parametros)}"

    parsed_redirect = urlparse(REDIRECT_URI)

    host = parsed_redirect.hostname or "localhost"
    port = parsed_redirect.port or 8000

    servidor = HTTPServer((host, port), CallbackHandler)

    print("Servidor local iniciado.")
    print("Abrindo página de autorização do Bling...")
    print("Autorize o aplicativo no navegador.")

    webbrowser.open(url)

    servidor.handle_request()
    servidor.server_close()

    if "error" in resultado_callback:
        raise RuntimeError(resultado_callback["error"])

    codigo = resultado_callback.get("code")

    if not codigo:
        raise RuntimeError("Não foi possível obter o código de autorização.")

    return codigo


def trocar_codigo_por_tokens(codigo):
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "enable-jwt": "1",
    }

    dados = {
        "grant_type": "authorization_code",
        "code": codigo,
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
            f"Erro ao obter tokens. HTTP {resposta.status_code}: "
            f"{resposta.text}"
        )

    return resposta.json()


def salvar_tokens(tokens):
    dados = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens.get("token_type"),
        "expires_in": tokens.get("expires_in"),
        "scope": tokens.get("scope"),
        "obtained_at": int(time.time()),
    }

    with open(TOKENS_FILE, "w", encoding="utf-8") as arquivo:
        json.dump(dados, arquivo, indent=4)

    print(f"Tokens salvos com sucesso em {TOKENS_FILE}.")
    print("Os tokens não serão exibidos no terminal.")


def main():
    print("=" * 60)
    print("AUTENTICAÇÃO BLING")
    print("=" * 60)

    codigo = obter_codigo_autorizacao()

    print("Autorização recebida.")
    print("Solicitando tokens ao Bling...")

    tokens = trocar_codigo_por_tokens(codigo)
    salvar_tokens(tokens)

    print()
    print("FASE 1 CONCLUÍDA")
    print("OAuth realizado com sucesso.")


if __name__ == "__main__":
    main()