"""Testes do ledger, sem acessar o Dropbox ou serviços externos.

Execute com fakeredis instalado como dependência de desenvolvimento.
"""
import threading
import unittest
from unittest.mock import patch

try:
    import fakeredis
except ImportError:
    fakeredis = None

from registro_entregas import (
    BloqueioOcupado, BloqueioPerdido, RegistroEntregasRedis, RegistroIndisponivel,
)
import registro_entregas


CHAVE = "35261002423640000300550050000037041531922135"
NOTA = {
    "chave": CHAVE,
    "sha256": "a" * 64,
    "destino": f"Filial_Ecommerce/Meli/{CHAVE}-nfe.xml",
    "cnpj_emitente": "02423640000300",
    "canal_bling": "205634610",
    "numero": "3704", "serie": "5",
    "emissao": "2026-10-29T08:19:00-03:00",
    "caminho_xml": "/dados/xml_originais/29-10-2026/nota.xml",
}


@unittest.skipUnless(fakeredis is not None, "Instale fakeredis para os testes Redis sem rede.")
class RegistroEntregasTests(unittest.TestCase):
    def setUp(self):
        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        self.registro = self.novo_registro()

    def novo_registro(self):
        return RegistroEntregasRedis("redis://nao-usar", cliente=fakeredis.FakeRedis(
            server=self.server, decode_responses=True))

    def test_registro_e_indice_nao_expiram_e_sao_lidos_por_nova_instancia(self):
        r = self.registro.registrar(NOTA)
        self.assertEqual(r["estado"], "pendente")
        self.assertEqual(r["tentativas"], 0)
        self.assertEqual(self.novo_registro().listar(), [r])
        self.assertEqual(self.redis.ttl(f"bling:dropbox:nfe:{CHAVE}"), -1)
        self.assertEqual(self.redis.ttl("bling:dropbox:chaves"), -1)
        self.assertEqual(self.redis.xlen("bling:dropbox:eventos"), 1)

    def test_reconsulta_nao_reabre_entrega_e_enriquece_seed_sem_hash(self):
        seed = {k: v for k, v in NOTA.items() if k not in {"sha256", "caminho_xml"}}
        seed.update(sha256="", caminho_xml="")
        self.registro.marcar_preexistente(seed, ["Filial_Ecommerce/Meli/XML Importados/nota.xml"])
        eventos = self.redis.xrange("bling:dropbox:eventos")
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0][1]["estado"], "entregue")
        r = self.registro.registrar({**NOTA, "estado": "pendente", "tentativas": 0})
        self.assertEqual(r["estado"], "entregue")
        self.assertEqual(r["sha256"], NOTA["sha256"])
        self.assertEqual(r["caminho_xml"], NOTA["caminho_xml"])
        self.assertEqual(self.registro.registrar(NOTA), r)

    def test_conflito_preserva_identidade_e_encaminha_revisao(self):
        self.registro.marcar_preexistente(NOTA, ["historico/nota.xml"])
        recebido = {**NOTA, "sha256": "b" * 64, "destino": "outra/nota.xml"}
        r = self.registro.registrar(recebido)
        self.assertEqual(r["estado"], "revisao")
        self.assertTrue(r["entrega_anterior_confirmada"])
        self.assertEqual(r["sha256"], NOTA["sha256"])
        self.assertEqual(r["destino"], NOTA["destino"])
        self.assertEqual(set(r["conflito_identidade"]), {"sha256", "destino"})
        self.assertEqual(self.registro.marcar_preexistente(recebido, ["outra/nota.xml"])["estado"], "revisao")

    def test_repeticao_do_cadastro_preserva_falha_e_tentativas(self):
        self.registro.registrar(NOTA)
        self.registro.atualizar(CHAVE, estado="em_envio")
        self.registro.atualizar(CHAVE, estado="falha", ultimo_erro="falha antes de iniciar upload")
        r = self.registro.registrar(NOTA)
        self.assertEqual((r["estado"], r["tentativas"]), ("falha", 1))
        self.assertIn("antes de iniciar", r["ultimo_erro"])

    def test_tentativa_interrompida_vira_incerta_ao_adquirir_lock_apos_reinicio(self):
        self.registro.registrar(NOTA)
        self.registro.atualizar(CHAVE, estado="em_envio")
        novo = self.novo_registro()
        with novo.bloqueio() as lock:
            self.assertTrue(lock.verificar())
            r = novo.obter(CHAVE)
            self.assertEqual((r["estado"], r["tentativas"]), ("incerto", 1))
            with self.assertRaises(ValueError):
                novo.atualizar(CHAVE, estado="em_envio")

    def test_leitura_para_simulacao_nao_recupera_nem_muda_registro(self):
        self.registro.registrar(NOTA)
        self.registro.atualizar(CHAVE, estado="em_envio")
        antes = {k: self.redis.dump(k) for k in self.redis.keys("*")}
        self.assertEqual(self.novo_registro().listar()[0]["estado"], "em_envio")
        self.assertEqual({k: self.redis.dump(k) for k in self.redis.keys("*")}, antes)

    def test_apenas_um_worker_adquire_lock(self):
        outro = self.novo_registro()
        with self.registro.bloqueio() as lock:
            self.assertTrue(lock.renovar())
            with self.assertRaises(BloqueioOcupado):
                with outro.bloqueio():
                    self.fail("Segundo worker entrou em execução concorrente")
        with outro.bloqueio() as lock:
            self.assertTrue(lock.verificar())

    def test_perda_do_lock_bloqueia_mutacao_e_nao_libera_lock_alheio(self):
        self.registro.registrar(NOTA)
        with self.assertRaises(BloqueioPerdido):
            with self.registro.bloqueio() as lock:
                self.redis.set("bling:dropbox:bloqueio", "outro-worker")
                with self.assertRaises(BloqueioPerdido):
                    lock.verificar()
                with self.assertRaises(BloqueioPerdido):
                    lock.renovar()
                with self.assertRaises(BloqueioPerdido):
                    self.registro.atualizar(CHAVE, estado="em_envio")
                with self.assertRaises(BloqueioPerdido):
                    self.registro.definir_proximo_lote_em(123456.0)
        self.assertEqual(self.redis.get("bling:dropbox:bloqueio"), "outro-worker")
        self.assertEqual(self.registro.obter(CHAVE)["estado"], "pendente")

    def test_cooldown_persiste_e_nao_pode_ser_antecipado(self):
        self.assertEqual(self.registro.obter_proximo_lote_em(), 0.0)
        with self.assertRaises(BloqueioPerdido):
            self.registro.definir_proximo_lote_em(1000)
        with self.registro.bloqueio():
            self.registro.definir_proximo_lote_em(1000)
            self.assertEqual(self.registro.definir_proximo_lote_em(900), 1000)
        novo = self.novo_registro()
        self.assertEqual(novo.obter_proximo_lote_em(), 1000)
        self.assertEqual(self.redis.ttl("bling:dropbox:proximo_lote_em"), -1)

    def test_concorrencia_de_cadastros_nao_perde_entrega_concluida(self):
        self.registro.marcar_preexistente(NOTA, ["historico/nota.xml"])
        barreira = threading.Barrier(8)
        erros = []

        def executar(indice):
            try:
                outro = self.novo_registro()
                barreira.wait(timeout=5)
                # Cada concorrente acrescenta um campo distinto, obrigando oito
                # transações de escrita sem perder o estado nem os demais campos.
                outro.registrar({**NOTA, f"campo_{indice}": indice})
            except Exception as erro:
                erros.append(erro)

        threads = [threading.Thread(target=executar, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(erros, [])
        self.assertEqual(len(self.registro.listar()), 1)
        resultado = self.registro.obter(CHAVE)
        self.assertEqual(resultado["estado"], "entregue")
        for i in range(8):
            self.assertEqual(resultado[f"campo_{i}"], i)
        self.assertEqual(self.redis.xlen("bling:dropbox:eventos"), 9)

    def test_lock_perdido_entre_leitura_e_exec_nao_grava_nota_ou_evento(self):
        self.registro.registrar(NOTA)
        normalizar = registro_entregas._normalizar
        chamadas = 0

        def normalizar_com_perda_do_lock(dados):
            nonlocal chamadas
            resultado = normalizar(dados)
            chamadas += 1
            # atualizar normaliza a entrada primeiro e o resultado da transação
            # depois de WATCH/GET. Neste segundo ponto simulamos outro dono.
            if chamadas == 2:
                self.redis.set("bling:dropbox:bloqueio", "outro-worker")
            return resultado

        with self.assertRaises(BloqueioPerdido):
            with self.registro.bloqueio():
                with patch.object(registro_entregas, "_normalizar", side_effect=normalizar_com_perda_do_lock):
                    self.registro.atualizar(CHAVE, estado="em_envio")
        self.assertEqual(self.registro.obter(CHAVE)["estado"], "pendente")
        self.assertEqual(self.redis.xlen("bling:dropbox:eventos"), 1)
        self.assertEqual(self.redis.get("bling:dropbox:bloqueio"), "outro-worker")

    def test_intervalo_corrompido_impede_ciclo_sem_zerar_o_controle(self):
        self.redis.set("bling:dropbox:proximo_lote_em", "nan")
        with self.assertRaises(RegistroIndisponivel):
            self.registro.obter_proximo_lote_em()
        with self.registro.bloqueio():
            with self.assertRaises(RegistroIndisponivel):
                self.registro.definir_proximo_lote_em(1000)
        self.assertEqual(self.redis.get("bling:dropbox:proximo_lote_em"), "nan")

    def test_indice_inconsistente_e_falha_redis_suspendem_o_controle(self):
        self.redis.sadd("bling:dropbox:chaves", CHAVE)
        with self.assertRaises(RegistroIndisponivel):
            self.registro.listar()
        self.redis.flushall()
        with patch.object(self.registro._cliente, "get", side_effect=ConnectionError("indisponivel")):
            with self.assertRaisesRegex(RegistroIndisponivel, "consultar"):
                self.registro.obter(CHAVE)

    def test_atualizacao_nao_reabre_entregue_nem_substitui_identidade(self):
        self.registro.marcar_preexistente(NOTA, "historico/nota.xml")
        with self.assertRaises(ValueError):
            self.registro.atualizar(CHAVE, estado="pendente")
        with self.assertRaises(ValueError):
            self.registro.atualizar(CHAVE, destino="novo/nota.xml")
        self.assertEqual(self.registro.obter(CHAVE)["estado"], "entregue")

    def test_dados_invalidos_nao_sao_persistidos(self):
        for chave in ["1" * 43, "a" * 44, "١" * 44, None]:
            with self.subTest(chave=chave), self.assertRaises(ValueError):
                self.registro.registrar({**NOTA, "chave": chave})
        with self.assertRaises(ValueError):
            self.registro.registrar({**NOTA, "sha256": "invalido"})
        self.assertEqual(self.registro.listar(), [])


if __name__ == "__main__":
    unittest.main()
