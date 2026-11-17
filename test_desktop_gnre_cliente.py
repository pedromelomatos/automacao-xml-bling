import base64
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from desktop_gnre_cliente import ClienteGNRE, carregar_configuracao, ler_arquivos, revalidar_bling, salvar_pdf
from desktop_gnre_tela import data_interface


class DesktopClienteTests(unittest.TestCase):
    def test_situacao_atual_bling_bloqueia_nota_cancelada_antes_da_emissao(self):
        nota = {'chave': '1' * 44, 'emissao': '2026-11-17', 'numero': '123'}
        for situacao in ('2', '5'):
            with patch('baixar_xmls.ClienteBling') as cliente, patch('bling_auth.obter_access_token', return_value='fixture'), patch(
                    'baixar_xmls.consultar_nfes', return_value=[{'chaveAcesso': nota['chave'], 'situacao': situacao}]):
                if situacao == '2':
                    with self.assertRaisesRegex(RuntimeError, 'fora do escopo'):
                        revalidar_bling([nota])
                else:
                    self.assertEqual(revalidar_bling([nota])[0]['chave'], nota['chave'])
                cliente.return_value.close.assert_called_once()

    def test_ssh_restrito_sem_shell_sem_repetir_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for nome in ('chave', 'known_hosts'):
                (base / nome).write_text('fixture')
            arquivo = base / 'config.json'
            arquivo.write_text(json.dumps({'servidor': '192.0.2.1', 'usuario': 'e-c', 'chave': 'chave', 'known_hosts': 'known_hosts'}))
            cliente = ClienteGNRE(arquivo)
            with patch('desktop_gnre_cliente.shutil.which', return_value='ssh'), patch('desktop_gnre_cliente.subprocess.run',
                    return_value=SimpleNamespace(returncode=0, stdout=b'{"ok":true,"resultado":{"versao":1}}')) as executar:
                self.assertEqual(cliente.pedir('conectar'), {'versao': 1})
            args = executar.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes', args)
            self.assertIn('BatchMode=yes', args)
            self.assertEqual(args[-1], 'gnre-desktop')
            self.assertNotIn('shell', executar.call_args.kwargs)
            arquivo.write_text(arquivo.read_text().replace('192.0.2.1', '-oProxyCommand=algo'))
            with self.assertRaisesRegex(RuntimeError, 'Servidor'):
                carregar_configuracao(arquivo)

    def test_zip_nao_extrai_caminhos_e_elimina_xmls_repetidos(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            arquivo = base / 'notas.zip'
            with zipfile.ZipFile(arquivo, 'w') as z:
                z.writestr('../../fora.xml', b'<nfe/>')
                z.writestr('repetida.xml', b'<nfe/>')
                z.writestr('nao-xml.txt', b'ignorar')
            notas = ler_arquivos([arquivo])
            self.assertEqual(len(notas), 1)
            self.assertEqual(notas[0]['nome'], 'fora.xml')
            self.assertFalse((base.parent / 'fora.xml').exists())

    def test_pdf_nao_substitui_outro_e_recusa_caminho_ou_hash_alterado(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = b'%PDF-fixture'
            resposta = {'nome': 'GNRE-00000000000000-NF-1-SC.pdf', 'sha256': hashlib.sha256(pdf).hexdigest(),
                        'conteudo': base64.b64encode(pdf).decode()}
            saida = salvar_pdf(resposta, tmp)
            self.assertEqual(salvar_pdf(resposta, tmp), saida)
            resposta['nome'] = '../arquivo.pdf'
            with self.assertRaisesRegex(RuntimeError, 'Nome'):
                salvar_pdf(resposta, tmp)
            resposta['nome'] = saida.name
            resposta['sha256'] = '0' * 64
            with self.assertRaisesRegex(RuntimeError, 'integridade'):
                salvar_pdf(resposta, tmp)

    def test_datas_da_previa_sao_explicitas_e_validas(self):
        self.assertEqual(data_interface('17/11/2026'), '2026-11-17')
        with self.assertRaises(RuntimeError):
            data_interface('31/02/2026')


if __name__ == '__main__':
    unittest.main()
