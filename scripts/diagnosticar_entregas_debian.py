"""Diagnóstico de leitura: não executa ciclos de envio nem grava no Redis."""

from collections import Counter
import json
from pathlib import PurePosixPath
import subprocess
import time

import integracao_dropbox as i
from transporte_dropbox import CHAVE, ErroLeitura, validar_caminho_relativo


def informar(etapa, **campos):
    print(json.dumps({"etapa": etapa, **campos}, ensure_ascii=False), flush=True)


def classificar_erro_rclone(texto):
    # Não devolver stderr: ele pode conter dados da autorização ou URLs com parâmetros.
    texto = texto.lower()
    categorias = {
        "rede_ou_timeout": ("timeout", "timed out", "deadline exceeded", "no such host",
                            "connection refused", "network is unreachable", "connection reset",
                            "temporary failure in name resolution", "tls handshake", "i/o timeout"),
        "autorizacao": ("invalid_access_token", "expired_access_token", "invalid_grant",
                        "invalid_refresh_token", "invalid_client", "unauthorized", "invalid token"),
        "permissao": ("missing_scope", "insufficient_permissions", "no_permission", "permission denied"),
        "limite_dropbox": ("too_many_requests", "rate_limit", "rate limit", "too many requests"),
        "destino_ausente": ("path/not_found", "directory not found", "root folder not found"),
    }
    return [categoria for categoria, termos in categorias.items() if any(t in texto for t in termos)] or ["erro_nao_classificado"]


def diagnosticar():
    etapa = "configuracao"
    try:
        cfg = i.carregar_configuracao()
        if not cfg:
            raise RuntimeError("Configuração ausente.")
        informar(etapa, habilitada=cfg["habilitada"], modo=cfg["modo"],
                 max_por_lote=cfg["max_por_lote"], intervalo=cfg["intervalo_lotes_segundos"])

        etapa = "redis"
        db = i.criar_registro(cfg)
        db.ping()
        estados = db.listar()
        informar(etapa, ping="OK", estados_totais=dict(Counter(n["estado"] for n in estados)),
                 intervalo_restante_segundos=round(max(0, db.obter_proximo_lote_em() - time.time())),
                 bloqueio_ttl_segundos=db._cliente.ttl(db._lock_key),
                 nao_entregues=[{k: n.get(k) for k in ("numero", "estado", "tentativas", "atualizado_em")}
                                for n in estados if n["estado"] != "entregue"])

        etapa = "arquivos_locais"
        metadados = i.carregar_metadados()
        historico = i.carregar_historico(cfg)
        pendentes = {n["chave"] for n in estados if n["estado"] != "entregue"}
        problemas = []
        for n in metadados:
            if n["chave"] not in pendentes:
                continue
            try:
                atual = i.campos_xml(n["caminho_xml"])
                if any(atual[k] != n.get(k) for k in ("chave", "cnpj_emitente", "emissao", "sha256")):
                    problemas.append({"numero": n.get("numero"), "problema": "identidade_ou_hash_divergente"})
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                problemas.append({"numero": n.get("numero"), "problema": "arquivo_ausente_ou_xml_invalido"})
        informar(etapa, metadados=len(metadados), problemas=problemas)

        etapa = "listagem_dropbox"
        transporte = i.criar_transporte(cfg)  # permitir_envio=False
        transporte._validar_instalacao(ErroLeitura)
        informar(etapa, estado="consultando", timeout_segundos=cfg["tempo_limite_segundos"])
        inicio = time.monotonic()
        try:
            resultado = transporte._executar([
                "lsf", transporte.remote, "--recursive", "--files-only",
                "--include", "*.xml", "--include", "*.XML", "--format", "p",
                "--retries", "1", "--low-level-retries", "1",
            ])
        except subprocess.TimeoutExpired:
            informar(etapa, estado="falha", causa=["timeout_da_listagem"],
                     duracao_segundos=round(time.monotonic() - inicio, 1))
            return 1
        if resultado.returncode != 0:
            informar(etapa, estado="falha", codigo_rclone=resultado.returncode,
                     causa=classificar_erro_rclone(resultado.stderr),
                     duracao_segundos=round(time.monotonic() - inicio, 1))
            return 1

        inventario = {}
        for linha in resultado.stdout.splitlines():
            if not linha:
                continue
            try:
                caminho = validar_caminho_relativo(linha)
                if not caminho.lower().endswith(".xml"):
                    raise ErroLeitura("Arquivo inesperado.")
            except ErroLeitura:
                raise
            except RuntimeError:
                informar(etapa, estado="falha", causa=["caminho_invalido_no_inventario"],
                         caminho_com_problema=linha[:400])
                return 1
            for chave in CHAVE.findall(PurePosixPath(caminho).stem):
                inventario.setdefault(chave, []).append(caminho)
        informar(etapa, estado="OK", chaves_remotas=len(inventario),
                 duracao_segundos=round(time.monotonic() - inicio, 1))

        etapa = "planejamento_sem_envio"
        plano = i.planejar(cfg, metadados, inventario, historico, estados)
        informar(etapa, resumo=plano["resumo"], lotes=len(plano["lotes"]),
                 uploads_executados=0, alteracoes_redis=0)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as erro:
        # Somente o tipo é exibido para exceções inesperadas; nunca traceback/segredos.
        informar(etapa, estado="falha", tipo_erro=type(erro).__name__,
                 uploads_executados=0, alteracoes_redis=0)
        return 1


if __name__ == "__main__":
    raise SystemExit(diagnosticar())
