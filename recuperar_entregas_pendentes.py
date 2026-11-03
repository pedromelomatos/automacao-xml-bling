"""Recadastra entregas omitidas usando XMLs locais; não baixa XMLs nem faz uploads."""

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

from baixar_xmls import ClienteBling, calcular_periodo, consultar_nfes, filtrar_emitidas_na_janela
from bling_auth import obter_access_token
import integracao_dropbox as integracao


def recuperar(data_consulta, *, aplicar=False, notas=None, cfg=None, root=None):
    cfg = cfg if cfg is not None else integracao.carregar_configuracao()
    if not cfg:
        raise RuntimeError("Configuração de entrega ausente.")
    root = Path(root or integracao.pasta_dados()).resolve()
    inicio, fim = calcular_periodo(data_consulta, True)
    if notas is None:
        cliente = ClienteBling(obter_access_token())
        try:
            notas = consultar_nfes(cliente, inicio, fim)
        finally:
            cliente.close()
    selecionadas, fora, invalidas = filtrar_emitidas_na_janela(notas, inicio, fim)
    existentes = {n["chave"]: n for n in integracao.carregar_metadados(root)}
    report = {
        "estado": "simulacao", "janela": data_consulta.isoformat(),
        "inicio": inicio.isoformat(), "fim_exclusivo": fim.isoformat(),
        "consultadas": len(notas), "elegiveis": len(selecionadas),
        "fora_da_janela_ou_situacao": fora, "metadados_existentes": 0,
        "novos_metadados": 0, "metadados_registrados": 0,
        "downloads_executados": 0, "uploads_executados": 0,
        "novas_notas": [], "notas_verificadas": [], "erros": [],
    }
    if invalidas:
        report["erros"].append({"motivo": "datas_invalidas", "quantidade": invalidas})
    novas = []
    vistas = set()
    for nota in selecionadas:
        chave = str(nota.get("chaveAcesso") or "")
        numero = str(nota.get("numero") or "")
        try:
            if not re.fullmatch(r"[0-9]{44}", chave):
                raise RuntimeError("Chave inválida.")
            if chave in vistas:
                continue
            vistas.add(chave)
            # São somente os originais da janela; cópias organizadas/GNRE não são usadas.
            path = root / "xml_originais" / data_consulta.strftime("%d-%m-%Y") / f"{chave}.xml"
            campos = integracao.campos_xml(path)
            canal = str((nota.get("loja") or {}).get("id") or "")
            rota = cfg["_rotas"].get((campos["cnpj_emitente"], canal))
            emissao = datetime.fromisoformat(campos["emissao"])
            emissao_local = emissao.astimezone(cfg["_inicio"].tzinfo).replace(tzinfo=None)
            if campos["chave"] != chave:
                raise RuntimeError("Chave do XML diverge da consulta.")
            if not rota:
                raise RuntimeError("Rota não cadastrada para emitente e canal.")
            if not inicio <= emissao_local < fim or emissao < cfg["_inicio"]:
                raise RuntimeError("XML fora da janela ou anterior ao corte da integração.")
            anterior = existentes.get(chave)
            if anterior:
                identidade = dict(campos, canal_bling=canal)
                if any(anterior.get(k) != identidade[k]
                       for k in ("chave", "sha256", "cnpj_emitente", "canal_bling")):
                    raise RuntimeError("Metadados existentes divergem do XML ou canal atual.")
                if str(anterior.get("situacao_bling")) not in integracao.SITUACOES_ENTREGA:
                    raise RuntimeError("Metadados existentes têm situação divergente; revisar.")
                report["metadados_existentes"] += 1
                report["notas_verificadas"].append({"chave": chave, "numero": numero})
                continue
            novas.append((path, nota))
            report["notas_verificadas"].append({"chave": chave, "numero": numero})
            report["novas_notas"].append({
                "chave": chave, "numero": numero, "situacao_bling": str(nota["situacao"]),
                "canal_bling": canal, "destino": f"{rota}/{chave}-nfe.xml",
            })
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as erro:
            report["erros"].append({"numero": numero, "chave": chave, "motivo": str(erro)})
    report["novos_metadados"] = len(novas)
    if report["erros"]:
        report["estado"] = "bloqueada"
        return report
    if aplicar:
        # Validar o conjunto inteiro antes de registrar. Repetir a recuperação é idempotente.
        for path, nota in novas:
            integracao.registrar_metadados_download(path, nota, data_dir=root)
            report["metadados_registrados"] += 1
        report["estado"] = "recuperacao_registrada"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=date.fromisoformat, help="janela diária encerrada às 14h, AAAA-MM-DD")
    parser.add_argument("--aplicar", action="store_true", help="registrar entregas omitidas na outbox local")
    parser.add_argument("--saida", type=Path)
    args = parser.parse_args(argv)
    try:
        report = recuperar(args.data, aplicar=args.aplicar)
        if args.saida:
            integracao.salvar_json_atomico(args.saida, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["erros"] else 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        print("Recuperação interrompida. Confira API, XMLs locais e configuração no Debian.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
