"""Testes da fila com XMLs locais, transporte falso e ledger Redis real/fakeredis.

Nenhum teste executa rclone, usa as credenciais do projeto ou acessa o Dropbox.
"""
import copy
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import fakeredis
except ImportError:
    fakeredis = None

import integracao_dropbox as integracao
from registro_entregas import BloqueioPerdido, RegistroEntregasRedis, RegistroIndisponivel
from transporte_dropbox import EntregaIncerta


CNPJ = "02423640000300"
CANAL = "205634610"
PASTA = "Filial_Ecommerce/Meli"


class BaseIntegracao(unittest.TestCase):
    def setUp(self):
        self.temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporaria.cleanup)
        self.base = Path(self.temporaria.name)
        self.dados = self.base / "dados"
        self.dados.mkdir()
        self.config_path = self.base / "configuracao_dropbox.json"
        self.config = {
            "habilitada": True, "modo": "envio",
            "inicio_emissao": "2026-10-30T00:00:00-03:00",
            "redis_url": "redis://nao-conectar", "redis_prefixo": "teste:integracao",
            "rclone_executavel": "nao-executar-rclone", "rclone_config": "nao-usar.conf",
            "remote": "bling_dropbox:", "max_por_lote": 5,
            "intervalo_lotes_segundos": 60, "intervalo_verificacao_segundos": 60,
            "tempo_limite_segundos": 120, "historico_inicial": "entregas/ja_entregues.json",
            "rotas": [{"cnpj_emitente": CNPJ, "canal_bling": CANAL, "pasta": PASTA}],
        }
        self.salvar_config()
        self.env = patch.dict(os.environ, {
            "AUTOMACAO_DATA_DIR": str(self.dados),
            "AUTOMACAO_DROPBOX_CONFIG": str(self.config_path),
            "REDIS_URL": "redis://nao-conectar",
            "RCLONE_CONFIG": "nao-usar.conf",
            "RCLONE_EXECUTAVEL": "nao-executar-rclone",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.salvar_historico([])

    def salvar_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def cfg(self):
        return integracao.carregar_configuracao(self.config_path)

    def salvar_historico(self, notas):
        path = self.dados / self.config["historico_inicial"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"notas": notas}), encoding="utf-8")

    def nova_nota(self, numero, *, cnpj=CNPJ, canal=CANAL,
                  emissao="2026-10-30T08:00:00-03:00", situacao="6"):
        chave = "35" + "2610" + cnpj + "55" + "005" + str(numero).zfill(9) + "1" + str(numero * 47).zfill(8)[-8:] + "0"
        assert len(chave) == 44
        raw = (
            '<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe>'
            f'<infNFe Id="NFe{chave}"><ide><nNF>{numero}</nNF><serie>5</serie>'
            f'<dhEmi>{emissao}</dhEmi></ide><emit><CNPJ>{cnpj}</CNPJ></emit>'
            '</infNFe></NFe><protNFe><infProt>'
            f'<chNFe>{chave}</chNFe><cStat>100</cStat>'
            '</infProt></protNFe></nfeProc>'
        ).encode("utf-8")
        path = self.dados / "xml_originais" / "30-10-2026" / f"{chave}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        nota_bling = {"id": numero + 100000, "chaveAcesso": chave,
                      "loja": {"id": canal}, "situacao": situacao}
        integracao.registrar_metadados_download(path, nota_bling, self.dados)
        row = next(n for n in integracao.carregar_metadados(self.dados) if n["chave"] == chave)
        return row, nota_bling


class SimulacaoEPlanejamentoTests(BaseIntegracao):
    def test_main_simular_so_consulta_mesmo_com_configuracao_de_envio(self):
        row, _ = self.nova_nota(9701)
        for modo in ("simulacao", "envio"):
            with self.subTest(modo=modo):
                self.config.update(modo=modo, habilitada=True)
                self.salvar_config()
                db = SimpleNamespace(**{name: Mock(side_effect=AssertionError("Mutação proibida em simulação"))
                    for name in ("bloqueio", "registrar", "atualizar", "marcar_preexistente", "definir_proximo_lote_em")})
                db.listar = Mock(return_value=[])
                transport = SimpleNamespace(listar_xmls=Mock(return_value={}),
                                            enviar=Mock(side_effect=AssertionError("Upload proibido em simulação")))
                outbox = self.dados / "entregas/metadados" / (row["chave"] + ".json")
                original = outbox.read_bytes()
                stdout = io.StringIO()
                with patch.object(integracao, "criar_registro", return_value=db), patch.object(
                    integracao, "criar_transporte", return_value=transport,
                ) as criar_transporte, patch.object(integracao, "salvar_json_atomico") as salvar, patch("sys.stdout", stdout):
                    code = integracao.main(["simular", "--config", str(self.config_path)])
                self.assertEqual(code, 0)
                report = json.loads(stdout.getvalue())
                self.assertEqual(report["resumo"], {"enviar": 1})
                self.assertEqual((report["uploads_executados"], report["alteracoes_redis"]), (0, 0))
                self.assertFalse(criar_transporte.call_args.kwargs.get("permitir_envio", False))
                db.listar.assert_called_once()
                for name in ("bloqueio", "registrar", "atualizar", "marcar_preexistente", "definir_proximo_lote_em"):
                    getattr(db, name).assert_not_called()
                transport.enviar.assert_not_called()
                transport.listar_xmls.assert_called_once()
                salvar.assert_not_called()
                self.assertEqual(outbox.read_bytes(), original)

    def test_simulacao_offline_nao_constroi_redis_nem_transporte(self):
        self.nova_nota(9702)
        inventory = self.base / "inventario.json"
        inventory.write_text('{"arquivos": []}', encoding="utf-8")
        with patch.object(integracao, "criar_registro") as redis, patch.object(
            integracao, "criar_transporte",
        ) as transporte, patch("sys.stdout", io.StringIO()):
            self.assertEqual(integracao.main(["simular", "--sem-redis", "--inventario", str(inventory)]), 0)
        redis.assert_not_called()
        transporte.assert_not_called()

    def test_planejador_bloqueia_rota_desconhecida_emitente_errado_corte_e_situacao5(self):
        desconhecida, _ = self.nova_nota(9711, canal="999999")
        outro_emitente, _ = self.nova_nota(9712, cnpj="02423640000130")
        antiga, _ = self.nova_nota(9713, emissao="2026-10-29T23:59:59-03:00")
        situacao5, _ = self.nova_nota(9714, situacao="5")
        report = integracao.planejar(self.cfg(), [desconhecida, outro_emitente, antiga, situacao5], {})
        bykey = {n["chave"]: n["decisao"] for n in report["notas"]}
        self.assertEqual(bykey[desconhecida["chave"]], "rota_desconhecida")
        self.assertEqual(bykey[outro_emitente["chave"]], "rota_desconhecida")
        self.assertEqual(bykey[antiga["chave"]], "anterior_ao_corte")
        self.assertEqual(bykey[situacao5["chave"]], "situacao_nao_autorizada")
        self.assertEqual(report["lotes"], [])

    def test_planejador_preserva_seed_antigo_e_estados_incerto_revisao_entregue(self):
        seed, _ = self.nova_nota(9721, emissao="2026-10-29T08:00:00-03:00")
        incerto, _ = self.nova_nota(9722)
        revisao, _ = self.nova_nota(9723)
        entregue, _ = self.nova_nota(9724)
        alterado, _ = self.nova_nota(9725)
        estados = [dict(row, estado=estado, destino=f"{PASTA}/{row['chave']}-nfe.xml")
                   for row, estado in ((incerto, "incerto"), (revisao, "revisao"),
                                       (entregue, "entregue"), (alterado, "entregue"))]
        estados[-1]["sha256"] = "a" * 64
        report = integracao.planejar(self.cfg(), [seed, incerto, revisao, entregue, alterado], {}, [seed], estados)
        bykey = {n["chave"]: n["decisao"] for n in report["notas"]}
        self.assertEqual(bykey[seed["chave"]], "ja_entregue")
        self.assertEqual(bykey[incerto["chave"]], "revisao")
        self.assertEqual(bykey[revisao["chave"]], "revisao")
        self.assertEqual(bykey[entregue["chave"]], "ja_entregue")
        self.assertEqual(bykey[alterado["chave"]], "revisao")
        self.assertEqual(report["lotes"], [])

    def test_hash_mudado_ou_mesma_chave_em_outro_canal_exige_revisao(self):
        mudada, _ = self.nova_nota(9731)
        outra_pasta, _ = self.nova_nota(9732)
        with Path(mudada["caminho_xml"]).open("ab") as arquivo:
            arquivo.write(b"<!--alterado-->")
        inventory = {outra_pasta["chave"]: [f"Filial_Ecommerce/MeliViapol/{outra_pasta['chave']}-nfe.xml"]}
        report = integracao.planejar(self.cfg(), [mudada, outra_pasta], inventory)
        self.assertEqual(report["resumo"], {"revisao": 2})
        self.assertEqual(report["lotes"], [])

    def test_outbox_relativa_funciona_apos_migracao_da_pasta_dados(self):
        row, _ = self.nova_nota(9741)
        target = self.base / "novo_host" / "dados"
        shutil.copytree(self.dados, target)
        rows = integracao.carregar_metadados(target)
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0]["caminho_xml"]), (target / row["arquivo_relativo"]).resolve())
        self.assertEqual(integracao.planejar(self.cfg(), rows, {})["resumo"], {"enviar": 1})

    def test_reregistro_com_hash_diferente_falha_sem_substituir_metadados(self):
        row, nota = self.nova_nota(9742)
        outbox = self.dados / "entregas/metadados" / (row["chave"] + ".json")
        original = outbox.read_bytes()
        path = Path(row["caminho_xml"])
        path.write_bytes(path.read_bytes() + b"<!--nova versao-->")
        with self.assertRaisesRegex(RuntimeError, "divergentes"):
            integracao.registrar_metadados_download(path, nota, self.dados)
        self.assertEqual(outbox.read_bytes(), original)

    def test_metadados_nao_podem_escapar_da_pasta_dados(self):
        row, nota = self.nova_nota(9743)
        outside = self.base / "fora.xml"
        outside.write_bytes(Path(row["caminho_xml"]).read_bytes())
        with self.assertRaises(RuntimeError):
            integracao.registrar_metadados_download(outside, nota, self.dados)
        outbox = self.dados / "entregas/metadados" / (row["chave"] + ".json")
        tampered = json.loads(outbox.read_text(encoding="utf-8"))
        tampered["arquivo_relativo"] = "../fora.xml"
        outbox.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            integracao.carregar_metadados(self.dados)


class TransporteFalso:
    def __init__(self, inventario=None):
        self.inventario = copy.deepcopy(inventario or {})
        self.tentativas = []
        self.leituras = 0
        self.ao_listar = None
        self.ao_enviar = None
        self.erro_envio = None

    def listar_xmls(self):
        self.leituras += 1
        if self.ao_listar:
            self.ao_listar(self.leituras)
        return copy.deepcopy(self.inventario)

    def enviar(self, caminho, destino, *, sha256_esperado=None):
        actual = hashlib.sha256(Path(caminho).read_bytes()).hexdigest()
        if actual != sha256_esperado:
            raise AssertionError("Motor não passou o hash registrado ao transporte.")
        self.tentativas.append(destino)
        if self.erro_envio:
            raise self.erro_envio
        chave = Path(destino).name.removesuffix("-nfe.xml")
        pasta, nome = destino.rsplit("/", 1)
        self.inventario[chave] = [f"{pasta}/XML Importados/{nome}"]
        if self.ao_enviar:
            self.ao_enviar(destino)


@unittest.skipUnless(fakeredis is not None, "Instale fakeredis para testar o ciclo Redis sem rede.")
class CiclosRedisTests(BaseIntegracao):
    def setUp(self):
        super().setUp()
        self.servidor = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.servidor, decode_responses=True)
        self.transporte = TransporteFalso()

    def registro(self):
        return RegistroEntregasRedis("redis://nao-conectar", prefixo="teste:integracao", cliente=
                                    fakeredis.FakeRedis(server=self.servidor, decode_responses=True))

    def ciclo(self, instante, registro=None):
        return integracao.executar_ciclo(self.cfg(), self.dados,
                                        registro=registro or self.registro(), transporte=self.transporte,
                                        agora=lambda: instante)

    def test_oito_notas_sao_cinco_depois_espera_persistida_depois_tres(self):
        for numero in range(9801, 9809):
            self.nova_nota(numero)
        first = self.ciclo(1000)
        self.assertEqual(first["uploads_executados"], 5)
        self.assertEqual(len(self.transporte.tentativas), 5)
        estados = self.registro().listar()
        self.assertEqual(sum(n["estado"] == "entregue" for n in estados), 5)
        self.assertEqual(sum(n["estado"] == "pendente" for n in estados), 3)
        self.assertEqual(self.registro().obter_proximo_lote_em(), 1060)
        second = self.ciclo(1059)
        self.assertEqual((second["estado"], second["uploads_executados"]), ("aguardando_intervalo", 0))
        self.assertEqual(len(self.transporte.tentativas), 5)
        third = self.ciclo(1060)
        self.assertEqual(third["uploads_executados"], 3)
        self.assertEqual(len(set(self.transporte.tentativas)), 8)
        self.assertEqual(self.ciclo(2000)["uploads_executados"], 0)
        self.assertEqual(len(self.transporte.tentativas), 8)

    def test_nota_no_historico_remoto_e_registrada_entregue_sem_upload(self):
        row, _ = self.nova_nota(9811)
        self.transporte.inventario[row["chave"]] = [f"{PASTA}/XML Importados/{row['chave']}-nfe.xml"]
        self.assertEqual(self.ciclo(1000)["uploads_executados"], 0)
        self.assertEqual(self.registro().obter(row["chave"])["estado"], "entregue")
        self.assertEqual(self.transporte.tentativas, [])

    def test_timeout_fica_incerto_e_nao_e_reenviado_apos_reinicio(self):
        row, _ = self.nova_nota(9812)
        self.transporte.erro_envio = EntregaIncerta("Timeout simulado")
        self.assertEqual(self.ciclo(1000)["uploads_executados"], 1)
        saved = self.registro().obter(row["chave"])
        self.assertEqual((saved["estado"], saved["tentativas"]), ("incerto", 1))
        self.transporte.erro_envio = None
        self.assertEqual(self.ciclo(5000)["uploads_executados"], 0)
        self.assertEqual(self.registro().obter(row["chave"])["estado"], "incerto")
        self.assertEqual(len(self.transporte.tentativas), 1)

    def test_redis_indisponivel_impede_ate_consulta_ou_upload_do_transporte(self):
        self.nova_nota(9813)
        self.servidor.connected = False
        with self.assertRaises(RegistroIndisponivel):
            self.ciclo(1000)
        self.assertEqual(self.transporte.leituras, 0)
        self.assertEqual(self.transporte.tentativas, [])

    def test_falha_na_gravacao_do_estado_impede_upload(self):
        self.nova_nota(9814)
        db = self.registro()
        with patch.object(db, "atualizar", side_effect=RegistroIndisponivel("Falha de persistência simulada")):
            with self.assertRaises(RegistroIndisponivel):
                self.ciclo(1000, db)
        self.assertEqual(self.transporte.tentativas, [])

    def test_perda_do_lock_durante_leitura_impede_primeiro_upload(self):
        self.nova_nota(9815)
        db = self.registro()
        def perder_na_leitura(numero):
            if numero == 2:
                self.redis.set(db._lock_key, "outro-worker", ex=300)
        self.transporte.ao_listar = perder_na_leitura
        with self.assertRaises(BloqueioPerdido):
            self.ciclo(1000, db)
        self.assertEqual(self.transporte.tentativas, [])
        self.assertEqual(self.redis.get(db._lock_key), "outro-worker")

    def test_perda_do_lock_apos_primeiro_upload_impede_qualquer_envio_extra(self):
        first, _ = self.nova_nota(9816)
        self.nova_nota(9817)
        db = self.registro()
        self.transporte.ao_enviar = lambda destino: self.redis.set(db._lock_key, "outro-worker", ex=300)
        with self.assertRaises(BloqueioPerdido):
            self.ciclo(1000, db)
        self.assertEqual(len(self.transporte.tentativas), 1)
        self.assertEqual(db.obter(first["chave"])["estado"], "em_envio")
        self.assertEqual(self.redis.get(db._lock_key), "outro-worker")


if __name__ == "__main__":
    unittest.main()
