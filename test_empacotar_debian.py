import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import empacotar_debian
import instalar_rclone


CHAVE = "35261002423640000300550050000037041531922135"


def criar_cache(cache):
    cache.mkdir(parents=True)
    arquivo = cache / instalar_rclone.NOME_ARQUIVO
    with zipfile.ZipFile(arquivo, "w") as pacote:
        pacote.writestr(instalar_rclone.MEMBRO_BINARIO, b"\x7fELF-binario-teste")
        pacote.writestr("../../fora-do-cache.txt", b"NAO-EXTRAIR")
    digest = hashlib.sha256(arquivo.read_bytes()).hexdigest()
    (cache / "SHA256SUMS").write_text(
        f"# SHA256 sums\n\n{digest}  {arquivo.name}\n", encoding="utf-8"
    )
    return arquivo


class RcloneInstalacaoTests(unittest.TestCase):
    def test_extrai_somente_binario_verificado_sem_rede(self):
        with tempfile.TemporaryDirectory() as pasta:
            root = Path(pasta)
            cache = root / "cache"
            criar_cache(cache)
            with patch.object(instalar_rclone, "obter_url") as rede:
                destino = instalar_rclone.instalar(cache, root / "bin/rclone")
            self.assertEqual(destino.read_bytes(), b"\x7fELF-binario-teste")
            self.assertFalse((root / "fora-do-cache.txt").exists())
            rede.assert_not_called()

    def test_checksum_divergente_nao_instala_nem_executa(self):
        with tempfile.TemporaryDirectory() as pasta:
            root = Path(pasta)
            arquivo = criar_cache(root / "cache")
            arquivo.write_bytes(b"arquivo-adulterado")
            with patch.object(instalar_rclone, "obter_url") as rede:
                with self.assertRaisesRegex(RuntimeError, "SHA-256 divergente"):
                    instalar_rclone.instalar(root / "cache", root / "bin/rclone")
            self.assertFalse((root / "bin/rclone").exists())
            rede.assert_not_called()


class DebianPacoteTests(unittest.TestCase):
    def criar_projeto(self, root):
        for nome in empacotar_debian.ARQUIVOS:
            destino = root / nome
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(empacotar_debian.BASE_DIR / nome, destino)
            if nome.endswith(".sh"):
                destino.write_bytes(destino.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        (root / "configuracao.json").write_text('{"unidades": {}}', encoding="utf-8")
        config = json.loads((root / "configuracao_dropbox.example.json").read_text(encoding="utf-8"))
        config.update(habilitada=True, modo="envio")
        (root / "configuracao_dropbox.example.json").write_text(json.dumps(config), encoding="utf-8")
        (root / "dependencias_docker").mkdir()
        (root / "dependencias_docker/redis-6.4.0-py3-none-any.whl").write_bytes(b"wheel-teste")
        (root / "dependencias_docker/tokens.json").write_text("SEGREDO-NAO-EMPACOTAR", encoding="utf-8")
        (root / "segredos/rclone").mkdir(parents=True)
        for nome in [".env", "tokens.json", "estado_execucao.json", "segredos/rclone/rclone.conf", "nota.xml"]:
            (root / nome).write_text("SEGREDO-NAO-EMPACOTAR", encoding="utf-8")
        historico = root / "historico.json"
        historico.write_text(json.dumps({"ja_entregues_no_recorte_28_29": [
            {"chave": CHAVE, "cnpj_emitente": "02423640000300", "canal_bling": "205634610",
             "destinatario": "PRIVADO-NAO-EMPACOTAR", "token": "SEGREDO-NAO-EMPACOTAR"}
        ]}), encoding="utf-8")
        cache = root / "cache-rclone"
        criar_cache(cache)
        return historico, cache

    def test_pacote_tem_manifesto_simulacao_e_sem_segredos(self):
        with tempfile.TemporaryDirectory() as pasta:
            root = Path(pasta)
            historico, cache = self.criar_projeto(root)
            with patch.object(instalar_rclone, "obter_url") as rede:
                destino = empacotar_debian.gerar_pacote(root, historico=historico, cache_rclone=cache)
            rede.assert_not_called()
            with zipfile.ZipFile(destino) as pacote:
                nomes = pacote.namelist()
                self.assertIsNone(pacote.testzip())
                for nome in nomes:
                    self.assertFalse(nome.startswith("segredos/") or nome.startswith("dados/"), nome)
                    self.assertNotIn(Path(nome).name, {".env", "tokens.json", "rclone.conf", "estado_execucao.json"})
                    self.assertFalse(nome.lower().endswith(".xml"), nome)
                    self.assertNotIn(b"SEGREDO-NAO-EMPACOTAR", pacote.read(nome), nome)
                    self.assertNotIn(b"PRIVADO-NAO-EMPACOTAR", pacote.read(nome), nome)
                    if nome.endswith(".sh"):
                        self.assertNotIn(b"\r\n", pacote.read(nome), nome)
                cfg = json.loads(pacote.read("configuracao_dropbox.example.json"))
                self.assertIs(cfg["habilitada"], False)
                self.assertEqual(cfg["modo"], "simulacao")
                seed = json.loads(pacote.read("bootstrap/ja_entregues.json"))["notas"]
                self.assertEqual(seed, [{"chave": CHAVE, "cnpj_emitente": "02423640000300", "canal_bling": "205634610"}])
                manifesto = json.loads(pacote.read("manifesto.json"))
                self.assertEqual(set(manifesto["arquivos_sha256"]) | {"manifesto.json"}, set(nomes))
                for nome, digest in manifesto["arquivos_sha256"].items():
                    self.assertEqual(hashlib.sha256(pacote.read(nome)).hexdigest(), digest, nome)

    def test_recusa_seed_com_emitente_divergente(self):
        with tempfile.TemporaryDirectory() as pasta:
            path = Path(pasta) / "historico.json"
            path.write_text(json.dumps({"notas": [{"chave": CHAVE, "cnpj_emitente": "02423640000130", "canal_bling": "205634610"}]}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                empacotar_debian.preparar_historico(path)

    def test_recusa_chaves_repetidas_no_bootstrap(self):
        with tempfile.TemporaryDirectory() as pasta:
            path = Path(pasta) / "historico.json"
            item = {"chave": CHAVE, "cnpj_emitente": "02423640000300", "canal_bling": "205634610"}
            path.write_text(json.dumps({"notas": [item, item]}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "repetidas"):
                empacotar_debian.preparar_historico(path)

    def test_instalador_nao_inicia_perfis_de_download_ou_entrega(self):
        script = (empacotar_debian.BASE_DIR / "docs/instalar_integracao_debian.sh").read_text(encoding="utf-8")
        self.assertIn("dc up -d --wait redis", script)
        self.assertNotIn("--profile", script)
        self.assertIn("integracao_dropbox.py simular", script)
        self.assertNotIn("integracao_dropbox.py sincronizar", script)
        self.assertNotIn("importar-entregues", script)
        self.assertNotIn("agendador_docker.py", script)


if __name__ == "__main__":
    unittest.main()
