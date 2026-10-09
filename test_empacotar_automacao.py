import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import empacotar_automacao as entrega


class PacoteTests(unittest.TestCase):
    def test_pacote_atual_sem_credenciais_nem_estado_e_com_manifesto(self):
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            for nome in entrega.CODIGOS:
                origem = entrega.BASE_DIR / nome
                if nome == "configuracao.json":
                    origem = entrega.BASE_DIR / "configuracao.example.json"
                shutil.copy2(origem, raiz / nome)
            for nome in ("instalar_automacao.ps1", "Instalar automacao.cmd", "Dockerfile", "docker-compose.yml", ".dockerignore"):
                shutil.copy2(entrega.BASE_DIR / nome, raiz / nome)
            instalador = raiz / "python-3.14.7-amd64.exe"
            instalador.write_bytes(b"instalador-de-teste")
            for nome in ("dependencias", "dependencias_docker"):
                (raiz / nome).mkdir()
                (raiz / nome / "biblioteca.whl").write_bytes(b"wheel-de-teste")
                (raiz / nome / "tokens.json").write_text("NAO-EMPACOTAR", encoding="utf-8")
            for nome in (".env", "tokens.json", "estado_execucao.json", "configuracao_execucao.json"):
                (raiz / nome).write_text("NAO-EMPACOTAR", encoding="utf-8")
            with patch.object(entrega, "HASH_PYTHON", hashlib.sha256(instalador.read_bytes()).hexdigest()):
                caminho = entrega.gerar_pacote(raiz)
            with zipfile.ZipFile(caminho) as z:
                self.assertIsNone(z.testzip())
                nomes = z.namelist()
                for proibido in (".env", "tokens.json", "estado_execucao.json", "configuracao_execucao.json"):
                    self.assertFalse(any(Path(n).name == proibido for n in nomes), proibido)
                    self.assertEqual((raiz / proibido).read_text(), "NAO-EMPACOTAR")
                for nome in entrega.CODIGOS:
                    self.assertEqual(z.read("arquivos-automacao/" + nome), (raiz / nome).read_bytes())
                manifesto = json.loads(z.read("manifesto.json"))["arquivos_sha256"]
                self.assertEqual(set(manifesto) | {"manifesto.json"}, set(nomes))
                for nome, esperado in manifesto.items():
                    self.assertEqual(hashlib.sha256(z.read(nome)).hexdigest(), esperado)
                compose = z.read("instalacao-docker/docker-compose.yml").decode()
                self.assertIn("../arquivos-automacao:/credenciais", compose)
                self.assertIn("BLING_TOKENS_FILE: /credenciais/tokens.json", compose)
                self.assertIn("../dados:/dados", compose)
                dockerfile = z.read("instalacao-docker/Dockerfile").decode()
                self.assertIn("arquivos-automacao/preparar_instalacao.py", dockerfile)
                self.assertIn("COPY instalacao-docker/dependencias/ /wheels/", dockerfile)
                self.assertIn("!arquivos-automacao/preparar_instalacao.py", z.read("instalacao-docker/Dockerfile.dockerignore").decode())


if __name__ == "__main__":
    unittest.main()
