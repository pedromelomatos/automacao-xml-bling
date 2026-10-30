"""Fila de entrega de NF-e ao Dropbox. Simulação não executa uploads nem altera Redis."""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent
NS = {"n": "http://www.portalfiscal.inf.br/nfe"}


def pasta_dados():
    explicit = os.getenv("AUTOMACAO_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    shared = BASE_DIR.parent / "dados"
    return shared.resolve() if shared.is_dir() else BASE_DIR


def caminho_configuracao():
    return Path(os.getenv("AUTOMACAO_DROPBOX_CONFIG", BASE_DIR / "configuracao_dropbox.json"))


def validar_relativo(valor):
    if not isinstance(valor, str) or not valor or "\\" in valor or ":" in valor or "\x00" in valor:
        raise RuntimeError("Caminho relativo inválido na integração Dropbox.")
    path = PurePosixPath(valor)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in valor.split("/")):
        raise RuntimeError("Caminho relativo inválido na integração Dropbox.")
    return valor


def carregar_configuracao(caminho=None):
    path = Path(caminho) if caminho else caminho_configuracao()
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError
        if type(cfg.get("habilitada")) is not bool or cfg.get("modo") not in {"simulacao", "envio"}:
            raise ValueError
        start = datetime.fromisoformat(cfg["inicio_emissao"])
        if start.tzinfo is None:
            raise ValueError
        for field, lower, upper in (("max_por_lote", 1, 5), ("intervalo_lotes_segundos", 1, 86400),
                                     ("intervalo_verificacao_segundos", 1, 3600), ("tempo_limite_segundos", 1, 300)):
            if type(cfg.get(field)) is not int or not lower <= cfg[field] <= upper:
                raise ValueError
        routes = {}
        for route in cfg["rotas"]:
            cnpj, channel = str(route["cnpj_emitente"]), str(route["canal_bling"])
            folder = validar_relativo(route["pasta"])
            if not re.fullmatch(r"\d{14}", cnpj) or not channel.isdecimal() or (cnpj, channel) in routes:
                raise ValueError
            if len(folder.split("/")) != 2 or folder.split("/")[0] not in {"Filial_Ecommerce", "Matriz_Ecommerce"}:
                raise ValueError
            routes[(cnpj, channel)] = folder
        if not routes or not re.fullmatch(r"[A-Za-z0-9_-]+:", cfg["remote"]):
            raise ValueError
        cfg["_rotas"] = routes
        cfg["_inicio"] = start
        cfg["redis_url"] = os.getenv("REDIS_URL", cfg["redis_url"])
        cfg["rclone_config"] = os.getenv("RCLONE_CONFIG", cfg["rclone_config"])
        cfg["rclone_executavel"] = os.getenv("RCLONE_EXECUTAVEL", cfg["rclone_executavel"])
        cfg["redis_prefixo"] = cfg.get("redis_prefixo", "bling:dropbox")
        validar_relativo(cfg["historico_inicial"])
        return cfg
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Configuração Dropbox inválida; confira formato, corte, rotas e limites.") from exc


def salvar_json_atomico(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def campos_xml(path):
    try:
        raw = Path(path).read_bytes()
        root = ET.fromstring(raw)
        inf = root.find(".//n:infNFe", NS)
        prot = root.find(".//n:protNFe/n:infProt", NS)
        if inf is None or prot is None:
            raise ValueError
        key = inf.get("Id", "").removeprefix("NFe")
        cnpj = inf.findtext("n:emit/n:CNPJ", namespaces=NS)
        issued = inf.findtext("n:ide/n:dhEmi", namespaces=NS)
        stamp = datetime.fromisoformat(issued)
        if (not re.fullmatch(r"\d{44}", key) or cnpj != key[6:20] or stamp.tzinfo is None
                or prot.findtext("n:chNFe", namespaces=NS) != key
                or prot.findtext("n:cStat", namespaces=NS) not in {"100", "150"}):
            raise ValueError
        return {"chave": key, "cnpj_emitente": cnpj, "emissao": issued,
                "numero": inf.findtext("n:ide/n:nNF", namespaces=NS),
                "serie": inf.findtext("n:ide/n:serie", namespaces=NS),
                "sha256": hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, TypeError, ET.ParseError) as exc:
        raise RuntimeError("XML ausente, incompleto ou sem autorização compatível para entrega.") from exc


def registrar_metadados_download(caminho, nota, data_dir=None):
    """Outbox local durável; executada só quando existe config da integração."""
    if not caminho_configuracao().exists():
        return
    root = Path(data_dir or pasta_dados()).resolve()
    path = Path(caminho).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError("XML fora da pasta de dados; metadados não registrados.") from exc
    record = campos_xml(path)
    if record["chave"] != str(nota.get("chaveAcesso", "")):
        raise RuntimeError("Chave do XML diverge da NF-e consultada no Bling.")
    record.update({"canal_bling": str((nota.get("loja") or {}).get("id") or ""),
                   "situacao_bling": str(nota.get("situacao") or ""),
                   "id_bling": nota.get("id"), "arquivo_relativo": relative})
    target = root / "entregas/metadados" / (record["chave"] + ".json")
    if target.exists():
        old = json.loads(target.read_text(encoding="utf-8"))
        if any(old.get(k) != record.get(k) for k in ("chave", "sha256", "cnpj_emitente", "canal_bling")):
            raise RuntimeError("Metadados divergentes para a mesma chave; revisão necessária.")
    salvar_json_atomico(target, record)


def carregar_metadados(data_dir=None):
    root = Path(data_dir or pasta_dados()).resolve()
    rows = []
    for path in sorted((root / "entregas/metadados").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if path.stem != record["chave"] or not re.fullmatch(r"\d{44}", record["chave"]):
                raise ValueError
            relative = validar_relativo(record["arquivo_relativo"])
            xml = (root / relative).resolve()
            xml.relative_to(root)
            record["caminho_xml"] = str(xml)
            rows.append(record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Metadados locais inválidos; ciclo suspenso para revisão.") from exc
    return rows


def carregar_historico(cfg, data_dir=None):
    path = Path(data_dir or pasta_dados()) / cfg["historico_inicial"]
    if not path.exists():
        # A estreia só é liberada com um histórico explícito, mesmo que vazio.
        raise RuntimeError("Histórico inicial ausente; reconcilie entregas antes de iniciar.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["notas"]
        if not isinstance(rows, list):
            raise ValueError
        for n in rows:
            key = n["chave"]
            if not re.fullmatch(r"\d{44}", key) or n["cnpj_emitente"] != key[6:20]:
                raise ValueError
        if len({n["chave"] for n in rows}) != len(rows):
            raise ValueError
        return rows
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Histórico inicial inválido; nenhum envio autorizado.") from exc


def planejar(cfg, registros, inventario, historico=(), estados=()):
    delivered = {n["chave"] for n in historico}
    db = {n["chave"]: n for n in estados}
    decisions = []
    for original in sorted(registros, key=lambda n: (n.get("emissao", ""), n["chave"])):
        row = dict(original)
        key = row["chave"]
        route = cfg["_rotas"].get((row.get("cnpj_emitente"), str(row.get("canal_bling"))))
        row["destino"] = f"{route}/{key}-nfe.xml" if route else ""
        previous = db.get(key, {})
        matches = inventario.get(key, [])
        if previous and any(previous.get(k) not in (None, "", row.get(k))
                            for k in ("sha256", "destino", "cnpj_emitente", "canal_bling")):
            decision = "revisao"
        elif key in delivered:
            decision = "ja_entregue"
        elif previous.get("estado") in {"entregue", "incerto", "em_envio", "revisao"}:
            decision = "ja_entregue" if previous["estado"] == "entregue" else "revisao"
        elif not route:
            decision = "rota_desconhecida"
        elif str(row.get("situacao_bling")) != "6":
            decision = "situacao_nao_autorizada"
        else:
            try:
                issued = datetime.fromisoformat(row["emissao"])
                if issued.tzinfo is None:
                    raise ValueError
                if issued < cfg["_inicio"]:
                    decision = "anterior_ao_corte"
                elif matches:
                    if all(p.rsplit("/", 1)[0] == route or p.startswith(route + "/XML Importados/") for p in matches):
                        decision = "ja_entregue"
                    else:
                        decision = "revisao"
                else:
                    actual = campos_xml(row["caminho_xml"])
                    if any(actual[k] != row.get(k) for k in ("chave", "sha256", "cnpj_emitente", "emissao")):
                        raise ValueError
                    if previous and any(previous.get(k) not in (None, "", row.get(k)) for k in ("sha256", "destino", "cnpj_emitente", "canal_bling")):
                        decision = "revisao"
                    else:
                        decision = "enviar"
            except (RuntimeError, ValueError, KeyError, TypeError):
                decision = "revisao"
        row["decisao"] = decision
        row["evidencias_remotas"] = matches
        decisions.append(row)
    candidates = [r for r in decisions if r["decisao"] == "enviar"]
    batches = [[n["chave"] for n in candidates[i:i+cfg["max_por_lote"]]]
               for i in range(0, len(candidates), cfg["max_por_lote"])]
    return {"simulacao": True, "uploads_executados": 0, "alteracoes_redis": 0,
            "resumo": dict(Counter(r["decisao"] for r in decisions)), "notas": decisions,
            "lotes": batches, "intervalo_lotes_segundos": cfg["intervalo_lotes_segundos"]}


def criar_transporte(cfg, permitir_envio=False):
    from transporte_dropbox import RcloneDropbox
    return RcloneDropbox(cfg["rclone_executavel"], cfg["rclone_config"], cfg["remote"],
                         cfg["tempo_limite_segundos"], permitir_envio=permitir_envio)


def criar_registro(cfg):
    from registro_entregas import RegistroEntregasRedis
    return RegistroEntregasRedis(cfg["redis_url"], prefixo=cfg["redis_prefixo"])


def executar_ciclo(cfg, data_dir=None, registro=None, transporte=None, agora=None):
    if not cfg["habilitada"] or cfg["modo"] != "envio":
        return {"estado": "envios_desabilitados", "uploads_executados": 0}
    from transporte_dropbox import EntregaIncerta, ErroTransporte
    root = Path(data_dir or pasta_dados())
    registro = registro or criar_registro(cfg)
    transporte = transporte or criar_transporte(cfg, permitir_envio=True)
    clock = agora or time.time
    total = 0
    with registro.bloqueio(ttl=max(300, cfg["tempo_limite_segundos"] + cfg["intervalo_lotes_segundos"] + 60)) as lock:
        # Reconciliar só por leitura antes de alterar estados ou iniciar transferências.
        inventory = transporte.listar_xmls()
        history = carregar_historico(cfg, root)
        for item in history:
            folder = cfg["_rotas"].get((item["cnpj_emitente"], str(item["canal_bling"])))
            if folder:
                seed = dict(item, destino=f"{folder}/{item['chave']}-nfe.xml", sha256=item.get("sha256", ""))
                registro.marcar_preexistente(seed, "historico_inicial_conferido")
        plan = planejar(cfg, carregar_metadados(root), inventory, history, registro.listar())
        for row in plan["notas"]:
            if row["decisao"] in {"anterior_ao_corte", "situacao_nao_autorizada"}:
                continue
            if not row["destino"]:
                # A outbox preserva o caso desconhecido para revisão sem inventar rota.
                continue
            saved = registro.registrar({k: v for k, v in row.items() if k not in {"decisao", "evidencias_remotas"}})
            if row["decisao"] == "ja_entregue" and saved["estado"] not in {"incerto", "em_envio", "revisao"}:
                registro.marcar_preexistente(row, "chave_no_historico_ou_inventario_remoto")
            elif row["decisao"] == "revisao" and saved["estado"] != "entregue":
                # Preservar incerto até conciliação explícita; não convertê-lo em pendente.
                if saved["estado"] not in {"incerto", "em_envio"}:
                    registro.atualizar(row["chave"], estado="revisao", ultimo_erro="Identidade, arquivo ou destino requer revisão.")
        if clock() < registro.obter_proximo_lote_em():
            return {"estado": "aguardando_intervalo", "uploads_executados": 0, "resumo": plan["resumo"]}
        candidates = [r for r in plan["notas"] if r["decisao"] == "enviar"]
        for row in candidates:
            if total >= cfg["max_por_lote"]:
                break
            current = registro.obter(row["chave"])
            if current["estado"] not in {"pendente", "falha"}:
                continue
            lock.renovar()
            lock.verificar()
            # Uma nova listagem reduz a janela de corrida com a operação manual.
            fresh = transporte.listar_xmls()
            if fresh.get(row["chave"]):
                paths = fresh[row["chave"]]
                folder = row["destino"].rsplit("/", 1)[0]
                if all(p.rsplit("/", 1)[0] == folder or p.startswith(folder + "/XML Importados/") for p in paths):
                    registro.marcar_preexistente(row, "chave_encontrada_antes_do_envio")
                else:
                    registro.atualizar(row["chave"], estado="revisao", ultimo_erro="Chave encontrada em outra pasta.")
                continue
            actual = campos_xml(row["caminho_xml"])
            if actual["sha256"] != row["sha256"]:
                registro.atualizar(row["chave"], estado="revisao", ultimo_erro="Conteúdo local mudou antes do envio.")
                continue
            lock.renovar()
            lock.verificar()
            registro.definir_proximo_lote_em(clock() + cfg["intervalo_lotes_segundos"])
            registro.atualizar(row["chave"], estado="em_envio", tentativas=int(current.get("tentativas", 0)) + 1)
            total += 1
            try:
                transporte.enviar(row["caminho_xml"], row["destino"], sha256_esperado=row["sha256"])
                lock.verificar()
                registro.atualizar(row["chave"], estado="entregue", evidencia="transferencia_rclone_concluida", ultimo_erro="")
            except (EntregaIncerta, ErroTransporte):
                registro.atualizar(row["chave"], estado="incerto", ultimo_erro="Transferência sem confirmação; requer conciliação, sem reenvio automático.")
                # Parar o lote evita multiplicar resultados incertos numa falha de conexão.
                break
            finally:
                registro.definir_proximo_lote_em(clock() + cfg["intervalo_lotes_segundos"])
    return {"estado": "ciclo_concluido", "uploads_executados": total, "resumo": plan["resumo"]}


def ler_inventario(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    inventory = {}
    for name in data["arquivos"]:
        validar_relativo(name)
        for match in re.finditer(r"(?<!\d)\d{44}(?!\d)", PurePosixPath(name).stem):
            inventory.setdefault(match.group(), []).append(name)
    return inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comando", choices=("simular", "sincronizar", "trabalhador", "status", "importar-entregues"))
    parser.add_argument("arquivo", nargs="?")
    parser.add_argument("--config")
    parser.add_argument("--inventario", help="snapshot local de inventário, apenas para simulação")
    parser.add_argument("--sem-redis", action="store_true", help="simulação offline; não consulta estados Redis")
    parser.add_argument("--saida", help="relatório local de simulação, sem efeitos remotos")
    args = parser.parse_args(argv)
    try:
        cfg = carregar_configuracao(args.config)
        if cfg is None:
            raise RuntimeError("Configuração Dropbox ausente.")
        root = pasta_dados()
        if args.comando == "simular":
            inventory = ler_inventario(args.inventario) if args.inventario else criar_transporte(cfg).listar_xmls()
            states = [] if args.sem_redis else criar_registro(cfg).listar()
            report = planejar(cfg, carregar_metadados(root), inventory, carregar_historico(cfg, root), states)
            report["redis_consultado"] = not args.sem_redis
            if args.saida:
                salvar_json_atomico(Path(args.saida), report)
        elif args.comando == "status":
            rows = criar_registro(cfg).listar()
            report = {"resumo": dict(Counter(n["estado"] for n in rows)), "notas": rows}
        elif args.comando == "importar-entregues":
            if not args.arquivo:
                raise RuntimeError("Informe um histórico explícito para importar entregas já realizadas.")
            # Não ler outro arquivo arbitrariamente como configuração: validar o mesmo schema.
            relative = Path(args.arquivo).resolve().relative_to(root.resolve()).as_posix()
            seed_cfg = dict(cfg, historico_inicial=relative)
            db = criar_registro(cfg)
            with db.bloqueio():
                count = 0
                for item in carregar_historico(seed_cfg, root):
                    folder = cfg["_rotas"].get((item["cnpj_emitente"], str(item["canal_bling"])))
                    if not folder:
                        continue
                    db.marcar_preexistente(dict(item, destino=f"{folder}/{item['chave']}-nfe.xml"), "historico_inicial_conferido")
                    count += 1
            report = {"entregas_preexistentes_registradas": count, "uploads_executados": 0}
        elif args.comando == "trabalhador":
            logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
            while True:
                delay = 60
                try:
                    cfg = carregar_configuracao(args.config)
                    delay = cfg["intervalo_verificacao_segundos"] if cfg else 60
                    if cfg and cfg["habilitada"] and cfg["modo"] == "simulacao":
                        report = planejar(cfg, carregar_metadados(root), criar_transporte(cfg).listar_xmls(),
                                          carregar_historico(cfg, root), criar_registro(cfg).listar())
                        logging.info("Simulação, sem uploads: %s", report["resumo"])
                    elif cfg:
                        report = executar_ciclo(cfg, root)
                        logging.info("Entrega: %s", report)
                except (OSError, RuntimeError, ValueError):
                    logging.error("Ciclo suspenso; confira configuração, Redis, arquivos e conexão. Sem reenvio de resultado incerto.")
                time.sleep(delay)
        else:
            report = executar_ciclo(cfg, root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        print("[ERRO] Integração interrompida; confira configuração, arquivos, Redis e conexão. Nenhuma credencial é exibida.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
