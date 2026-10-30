import hashlib
import json
import shutil
import subprocess
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
            (raiz / "configuracao_dropbox.json").write_text(
                '{"habilitada":true,"modo":"envio","token":"NAO-EMPACOTAR"}', encoding="utf-8",
            )
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
                cfg = json.loads(z.read("arquivos-automacao/configuracao_dropbox.json"))
                self.assertIs(cfg["habilitada"], False)
                self.assertEqual(cfg["modo"], "simulacao")
                self.assertNotIn("token", cfg)
                self.assertIn("NAO-EMPACOTAR", (raiz / "configuracao_dropbox.json").read_text())
                manifesto = json.loads(z.read("manifesto.json"))["arquivos_sha256"]
                self.assertEqual(set(manifesto) | {"manifesto.json"}, set(nomes))
                for nome, esperado in manifesto.items():
                    self.assertEqual(hashlib.sha256(z.read(nome)).hexdigest(), esperado)
                compose = z.read("instalacao-docker/docker-compose.yml").decode()
                self.assertIn("../arquivos-automacao:/credenciais", compose)
                self.assertIn("BLING_TOKENS_FILE: /credenciais/tokens.json", compose)
                self.assertIn("RCLONE_CONFIG: /credenciais/rclone/rclone.conf", compose)
                self.assertIn("../dados:/dados", compose)
                self.assertIn("../arquivos-automacao/configuracao_dropbox.json:/app/configuracao_dropbox.json:ro", compose)
                dockerfile = z.read("instalacao-docker/Dockerfile").decode()
                self.assertIn("arquivos-automacao/preparar_instalacao.py", dockerfile)
                self.assertIn("COPY instalacao-docker/dependencias/ /wheels/", dockerfile)
                for nome in ("instalar_rclone.py", "integracao_dropbox.py", "registro_entregas.py", "transporte_dropbox.py", "configuracao_dropbox.example.json"):
                    self.assertIn("arquivos-automacao/" + nome, dockerfile)
                for linha in dockerfile.splitlines():
                    if linha.startswith("COPY "):
                        for fonte in linha.split()[1:-1]:
                            self.assertTrue(fonte in nomes or any(n.startswith(fonte) for n in nomes), fonte)
                self.assertIn("!arquivos-automacao/preparar_instalacao.py", z.read("instalacao-docker/Dockerfile.dockerignore").decode())
                self.assertIn("--profile download up -d --build automacao-xml-bling", z.read("instalacao-docker/LEIA-ME.txt").decode())
                self.assertNotIn(b"NAO-EMPACOTAR", b"".join(z.read(n) for n in nomes))
                if shutil.which("docker"):
                    extraido = raiz / "extraido"
                    z.extractall(extraido)
                    (extraido / "arquivos-automacao/.env").write_text("", encoding="utf-8")
                    resultado = subprocess.run(
                        ["docker", "compose", "config", "--quiet"],
                        cwd=extraido / "instalacao-docker", capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(resultado.returncode, 0, resultado.stderr)

    def test_adaptacao_preserva_destinos_copy_e_reescreve_todas_as_fontes(self):
        original = (
            "FROM python:3.14.7-slim-bookworm\n"
            "COPY instalar_rclone.py ./\n"
            "COPY integracao_dropbox.py registro_entregas.py transporte_dropbox.py ./\n"
            "COPY configuracao_dropbox.example.json ./configuracao_dropbox.json\n"
            "ENV RCLONE_CONFIG=/segredos/rclone/rclone.conf\n"
        )
        resultado = entrega.adaptar_dockerfile(original)
        self.assertIn("COPY arquivos-automacao/instalar_rclone.py ./", resultado)
        self.assertIn("COPY arquivos-automacao/configuracao_dropbox.example.json ./configuracao_dropbox.json", resultado)
        self.assertIn("RCLONE_CONFIG=/credenciais/rclone/rclone.conf", resultado)
        self.assertNotRegex(resultado, r"(?m)^COPY (?:integracao_dropbox|registro_entregas|transporte_dropbox)\.py")


if __name__ == "__main__":
    unittest.main()
