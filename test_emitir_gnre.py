import base64
from contextlib import closing
from datetime import date
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lxml import etree

import emitir_gnre as emitir
from conferir_pagamento_gnre import digito
from preparar_gnre import GNRE
from test_preparar_gnre import CHAVE, NFE, configuracao, configuracao_uf_nova, preparar_lote, preparar_lote_pr, preparar_lote_sc, preparar_lote_uf


RECIBO = "20260000000001"


def codigo_exemplo(dados):
    from decimal import Decimal
    valor = sum(Decimal(v.text) for v in dados.findall(f'.//{{{GNRE}}}valor') if v.get('tipo') == '11')
    base = '858' + f'{int(valor * 100):011d}' + '0291' + '0' * 24 + emitir.texto(dados, 'identificadorGuia')
    base = base[:43]
    codigo = base[:3] + digito(base, 11) + base[3:]
    linha = ''.join(codigo[i:i + 11] + digito(codigo[i:i + 11], 11) for i in range(0, 44, 11))
    return codigo, linha


def pdf_exemplo(conteudo, homologacao=True):
    linhas = []
    for dados in etree.fromstring(conteudo).findall(f'.//{{{GNRE}}}TDadosGNRE'):
        _, linha = codigo_exemplo(dados)
        cnpj, _, identidade = emitir.identidade_guia(dados, CHAVE)
        _, uf, receita, _ = identidade.split('|')
        vencimento = emitir.texto(dados.find(f'.//{{{GNRE}}}item'), 'dataVencimento')
        linhas += ['Guia Nacional de Recolhimento de Tributos Estaduais - GNRE',
                   uf + ' ' + receita, cnpj, '/'.join(reversed(vencimento.split('-'))), linha]
        if homologacao:
            linhas += ['Guia Invalida para Pagamento']
    stream = ('BT /F1 9 Tf 20 800 Td 12 TL ' + ' T* '.join('(' + l + ') Tj' for l in linhas) + ' ET').encode()
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
    ]
    pdf = b"%PDF-1.4\n"
    offsets = [0]
    for numero, objeto in enumerate(objetos, 1):
        offsets.append(len(pdf))
        pdf += str(numero).encode() + b" 0 obj\n" + objeto + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 6\n0000000000 65535 f \n"
    pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return pdf + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"


def soap(conteudo):
    return (
        f'<s:Envelope xmlns:s="{emitir.consulta.SOAP}"><s:Body>'.encode() +
        conteudo + b"</s:Body></s:Envelope>"
    )


def recepcao():
    return soap(f"""<TRetLote_GNRE xmlns="{GNRE}"><ambiente>2</ambiente>
    <situacaoRecepcao><codigo>100</codigo><descricao>Recebido</descricao></situacaoRecepcao>
    <recibo><numero>{RECIBO}</numero></recibo></TRetLote_GNRE>""".encode())


def resultado(lote, codigo="402", status="0", pdf=None):
    raiz = etree.Element(f"{{{GNRE}}}TResultLote_GNRE", nsmap={None: GNRE})
    etree.SubElement(raiz, f"{{{GNRE}}}ambiente").text = "2"
    etree.SubElement(raiz, f"{{{GNRE}}}numeroRecibo").text = RECIBO
    situacao = etree.SubElement(raiz, f"{{{GNRE}}}situacaoProcess")
    etree.SubElement(situacao, f"{{{GNRE}}}codigo").text = codigo
    etree.SubElement(situacao, f"{{{GNRE}}}descricao").text = "Resultado do processamento"
    if codigo in ("402", "403"):
        resultados = etree.SubElement(raiz, f"{{{GNRE}}}resultado")
        for dados in etree.fromstring(lote).findall(f".//{{{GNRE}}}TDadosGNRE"):
            guia = etree.SubElement(resultados, f"{{{GNRE}}}guia", versao="2.00")
            etree.SubElement(guia, f"{{{GNRE}}}situacaoGuia").text = status
            guia.extend(list(dados))
            if status == "0":
                codigo_barras, linha = codigo_exemplo(guia)
                etree.SubElement(guia, f"{{{GNRE}}}linhaDigitavel").text = linha
                etree.SubElement(guia, f"{{{GNRE}}}codigoBarras").text = codigo_barras
        if pdf is not None:
            etree.SubElement(resultados, f"{{{GNRE}}}pdfGuias").text = base64.b64encode(pdf).decode()
    return soap(etree.tostring(raiz))


class ServicoFalso:
    ambiente = "2"
    timeout = 90

    def __init__(self, respostas):
        self.respostas = iter(respostas)
        self.chamadas = []

    def preparar(self, servico):
        pass

    def configuracao(self, uf, receita):
        return configuracao(receita)

    def chamar(self, servico, dados):
        self.chamadas.append(servico)
        retorno = next(self.respostas)
        if isinstance(retorno, Exception):
            raise retorno
        return retorno


class EmissaoGnreTests(unittest.TestCase):
    def test_ce_df_exigem_chave_da_nfe_original_e_conferem_numero_no_retorno(self):
        for uf in ("CE", "DF"):
            with self.subTest(uf=uf), tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
                conteudo = preparar_lote_uf(pasta, uf)
                with self.assertRaisesRegex(RuntimeError, "chave de NF-e válida"):
                    emitir.identificar_lote(conteudo)
                chaves = {"1": CHAVE}
                lote = emitir.registrar_lote(con, Path(pasta), "homologacao", conteudo, chaves)
                self.assertIn(CHAVE, lote["origens"])
                servico = ServicoFalso([recepcao(), resultado(conteudo, pdf=pdf_exemplo(conteudo)).replace(b">3704<", b">000003704<")])
                emitir.receber(con, lote, servico)
                con.execute("UPDATE lotes SET proxima_consulta=0 WHERE id=?", (lote["id"],))
                con.commit()
                atual = con.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
                self.assertTrue(emitir.consultar_lote(con, atual, servico))
                divergente = resultado(conteudo, pdf=pdf_exemplo(conteudo)).replace(b">3704<", b">3705<")
                with self.assertRaisesRegex(RuntimeError, "número do documento"):
                    emitir.processar_resultado(con, atual, divergente, "2")
                outra_chave = CHAVE[:22] + "006" + CHAVE[25:]
                with self.assertRaisesRegex(RuntimeError, "outra NF-e"):
                    emitir.registrar_lote(con, Path(pasta), "homologacao", conteudo, {"1": outra_chave})

    def test_ufs_conferem_chave_extra_e_recusam_campo_ausente_ou_repetido(self):
        for uf, campo in (("MA", "113"), ("PB", "120"), ("GO", "121"), ("MS", "105")):
            with self.subTest(uf=uf), tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
                conteudo = preparar_lote_uf(pasta, uf)
                lote = emitir.registrar_lote(con, Path(pasta), "homologacao", conteudo)
                servico = ServicoFalso([recepcao(), resultado(conteudo, pdf=pdf_exemplo(conteudo))])
                emitir.receber(con, lote, servico)
                con.execute("UPDATE lotes SET proxima_consulta=0 WHERE id=?", (lote["id"],))
                con.commit()
                atual = con.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
                self.assertTrue(emitir.consultar_lote(con, atual, servico))
                sem_campo = conteudo.replace(("<codigo>" + campo + "</codigo>").encode(), b"<codigo>99</codigo>")
                with self.assertRaisesRegex(RuntimeError, "campo adicional " + campo):
                    emitir.identificar_lote(sem_campo)
                raiz = etree.fromstring(conteudo)
                extras = raiz.find(f".//{{{GNRE}}}camposExtras")
                extras.append(etree.fromstring(etree.tostring(extras[0])))
                with self.assertRaisesRegex(RuntimeError, "única chave"):
                    emitir.identificar_lote(etree.tostring(raiz))
                if uf == "MS":
                    retorno = etree.fromstring(resultado(conteudo, pdf=pdf_exemplo(conteudo)))
                    item = retorno.find(f".//{{{GNRE}}}guia/{{{GNRE}}}itensGNRE/{{{GNRE}}}item")
                    item.remove(item.find(f"{{{GNRE}}}camposExtras"))
                    with self.assertRaisesRegex(RuntimeError, "campo adicional 105"):
                        emitir.processar_resultado(con, atual, etree.tostring(retorno), "2")

    def test_pb_recupera_pdf_do_mesmo_recibo_quando_uf_omite_a_chave(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as con:
            conteudo = preparar_lote_uf(pasta, "PB")
            lote = emitir.registrar_lote(con, Path(pasta), "homologacao", conteudo)
            emitir.receber(con, lote, ServicoFalso([recepcao()]))
            lote = con.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            resposta = etree.fromstring(resultado(conteudo, pdf=pdf_exemplo(conteudo)))
            item = resposta.find(f".//{{{GNRE}}}guia/{{{GNRE}}}itensGNRE/{{{GNRE}}}item")
            item.remove(item.find(f"{{{GNRE}}}camposExtras"))
            for campo, valor in ((f"{{{GNRE}}}valor", "7.77"), (f"{{{GNRE}}}dataVencimento", "2026-11-17")):
                elemento = item.find(campo)
                original = elemento.text
                elemento.text = valor
                emitir.processar_resultado(con, lote, etree.tostring(resposta), "2")
                self.assertEqual(con.execute('SELECT estado FROM lotes').fetchone()[0], 'em_revisao')
                self.assertFalse(list(Path(lote['pasta']).glob('guias-*.pdf')))
                elemento.text = original
            self.assertTrue(emitir.processar_resultado(con, lote, etree.tostring(resposta), "2"))
            self.assertEqual((Path(lote["pasta"]) / f"guias-{RECIBO}.pdf").read_bytes(), pdf_exemplo(conteudo))
            import json
            resumo = json.loads((Path(lote["pasta"]) / "resumo.json").read_text())
            self.assertTrue(resumo["guias"][0]["chave_omitida_pela_uf"])

    def test_sc_confere_documento_tipo24_e_persiste_pdf(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as registro:
            conteudo = preparar_lote_sc(pasta)
            lote = emitir.registrar_lote(registro, Path(pasta), "homologacao", conteudo)
            servico = ServicoFalso([recepcao(), resultado(conteudo, pdf=pdf_exemplo(conteudo))])
            emitir.receber(registro, lote, servico)
            registro.execute("UPDATE lotes SET proxima_consulta=0 WHERE id=?", (lote["id"],))
            registro.commit()
            atual = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            self.assertTrue(emitir.consultar_lote(registro, atual, servico))
            self.assertEqual((Path(lote["pasta"]) / f"guias-{RECIBO}.pdf").read_bytes(), pdf_exemplo(conteudo))
            for outro_tipo in ("22", "24"):
                outro = conteudo.replace(b'tipo="24"', f'tipo="{outro_tipo}"'.encode())
                self.assertEqual(emitir.identificar_lote(outro)[1], emitir.identificar_lote(conteudo)[1])

    def test_parana_confere_chave_extra_e_salva_pdf_do_retorno(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as registro:
            conteudo = preparar_lote_pr(pasta)
            lote = emitir.registrar_lote(registro, Path(pasta), "homologacao", conteudo)
            resposta = resultado(conteudo, pdf=pdf_exemplo(conteudo)).replace(b">3704<", b">000003704<")
            servico = ServicoFalso([recepcao(), resposta])
            emitir.receber(registro, lote, servico)
            registro.execute("UPDATE lotes SET proxima_consulta=0 WHERE id=?", (lote["id"],))
            registro.commit()
            atual = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            self.assertTrue(emitir.consultar_lote(registro, atual, servico))
            self.assertEqual((Path(lote["pasta"]) / f"guias-{RECIBO}.pdf").read_bytes(), pdf_exemplo(conteudo))
            outro = etree.fromstring(conteudo)
            documento = outro.find(f".//{{{GNRE}}}documentoOrigem")
            documento.set("tipo", "22")
            documento.text = CHAVE
            with self.assertRaisesRegex(RuntimeError, "Já existe envio"):
                emitir.registrar_lote(registro, Path(pasta), "homologacao", etree.tostring(outro))

    def test_parana_recusa_chave_ausente_ou_numero_do_documento_divergente(self):
        with tempfile.TemporaryDirectory() as pasta:
            conteudo = preparar_lote_pr(pasta)
        for alterado in (
            conteudo.replace(b">3704<", b">3705<"),
            conteudo.replace(b"<codigo>66</codigo>", b"<codigo>61</codigo>"),
            conteudo.replace(CHAVE.encode(), CHAVE.replace("55", "57", 1).encode()),
        ):
            with self.subTest(xml=alterado):
                with self.assertRaises(RuntimeError):
                    emitir.identificar_lote(alterado)

    def test_recibo_processamento_pdf_e_retomada_sem_novo_envio(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as registro:
            conteudo = preparar_lote(pasta)
            lote = emitir.registrar_lote(registro, Path(pasta), "homologacao", conteudo)
            servico = ServicoFalso([recepcao(), resultado(conteudo, pdf=pdf_exemplo(conteudo))])
            self.assertEqual(emitir.receber(registro, lote, servico), RECIBO)
            registro.execute("UPDATE lotes SET proxima_consulta=0 WHERE id=?", (lote["id"],))
            registro.commit()
            lote = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            self.assertTrue(emitir.consultar_lote(registro, lote, servico))
            self.assertEqual((Path(lote["pasta"]) / f"guias-{RECIBO}.pdf").read_bytes(), pdf_exemplo(conteudo))
            self.assertEqual(emitir.acompanhar(registro, lote["id"], servico, 0), 0)
            repetido = emitir.registrar_lote(registro, Path(pasta), "homologacao", conteudo)
            self.assertEqual(emitir.receber(registro, repetido, servico), RECIBO)
            self.assertEqual(servico.chamadas, ["recepcao", "resultado"])

    def test_timeout_de_envio_bloqueia_repeticao_mesmo_mudando_datas(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as registro:
            lote = emitir.registrar_lote(registro, Path(pasta), "homologacao", preparar_lote(pasta))
            servico = ServicoFalso([TimeoutError("Sem resposta")])
            with self.assertRaises(TimeoutError):
                emitir.receber(registro, lote, servico)
            lote = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            self.assertEqual(lote["estado"], "indeterminado")
            with self.assertRaisesRegex(RuntimeError, "tentativa de envio"):
                emitir.receber(registro, lote, servico)
            outro = preparar_lote(pasta, vencimento=date(2026, 11, 13))
            with self.assertRaisesRegex(RuntimeError, "Já existe envio"):
                emitir.registrar_lote(registro, Path(pasta), "homologacao", outro)
            self.assertEqual(servico.chamadas, ["recepcao"])

    def test_recupera_recibo_ja_persistido_antes_de_interrupcao(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as registro:
            lote = emitir.registrar_lote(registro, Path(pasta), "homologacao", preparar_lote(pasta))
            registro.execute("UPDATE lotes SET estado='enviando',recepcao=? WHERE id=?", (recepcao(), lote["id"]))
            registro.commit()
            lote = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            servico = ServicoFalso([])
            self.assertEqual(emitir.receber(registro, lote, servico), RECIBO)
            self.assertEqual(servico.chamadas, [])

    def test_pdf_invalido_preserva_o_recibo_e_nao_marca_conclusao(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as registro:
            conteudo = preparar_lote(pasta)
            lote = emitir.registrar_lote(registro, Path(pasta), "homologacao", conteudo)
            servico = ServicoFalso([recepcao(), resultado(conteudo, pdf=b"arquivo-invalido")])
            emitir.receber(registro, lote, servico)
            registro.execute("UPDATE lotes SET proxima_consulta=0 WHERE id=?", (lote["id"],))
            registro.commit()
            lote = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            with self.assertRaisesRegex(RuntimeError, "PDF completo"):
                emitir.consultar_lote(registro, lote, servico)
            atual = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
            self.assertEqual(atual["recibo"], RECIBO)
            self.assertEqual(atual["estado"], "em_revisao")
            self.assertFalse(list(Path(lote["pasta"]).glob("*.pdf")))

    def test_recepcao_desliga_repeticao_do_transporte_http(self):
        servico = emitir.ServicoGnre.__new__(emitir.ServicoGnre)
        servico.host, servico.timeout, servico.contexto = "gnre.example", 90, object()
        servico.contratos = {"GnreLoteRecepcao": ("https://namespace.example", "https://action.example")}
        with patch.object(emitir.consulta, "requisitar", side_effect=RuntimeError("HTTP 502")) as requisitar:
            with self.assertRaises(RuntimeError):
                servico.chamar("recepcao", b'<TLote_GNRE xmlns="http://www.gnre.pe.gov.br" versao="2.00"/>')
        self.assertEqual(requisitar.call_count, 1)
        self.assertEqual(requisitar.call_args.kwargs["tentativas"], 1)

    def test_producao_exige_confirmacao_antes_de_carregar_certificado(self):
        with patch.object(sys, "argv", [
            "emitir_gnre.py", "--ambiente", "producao", "enviar", "--lote", "lote.xml",
        ]), patch.object(emitir, "ServicoGnre") as servico:
            with self.assertRaises(SystemExit):
                emitir.main()
        servico.assert_not_called()

    def test_executar_pelo_cli_prepara_envia_acompanha_e_salva_pdf(self):
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            arquivo = raiz / "nota.xml"
            arquivo.write_bytes(NFE)
            lote = preparar_lote(pasta)
            servico = ServicoFalso([recepcao(), resultado(lote, pdf=pdf_exemplo(lote))])
            relogio = SimpleNamespace(agora=1000.0)
            def esperar(segundos):
                relogio.agora += segundos
            with patch.object(sys, "argv", [
                "emitir_gnre.py", "--dados", str(raiz / "saida"), "executar",
                "--xml", str(arquivo), "--vencimento", "2026-11-12", "--pagamento", "2026-11-12",
            ]), patch.object(emitir, "ServicoGnre", return_value=servico), patch.object(
                emitir.time, "time", side_effect=lambda: relogio.agora
            ), patch.object(emitir.time, "monotonic", side_effect=lambda: relogio.agora), patch.object(
                emitir.time, "sleep", side_effect=esperar
            ):
                self.assertEqual(emitir.main(), 0)
            self.assertEqual(servico.chamadas, ["recepcao", "resultado"])
            arquivos = list((raiz / "saida/homologacao").rglob("*.pdf"))
            self.assertEqual(len(arquivos), 1)
            self.assertEqual(arquivos[0].read_bytes(), pdf_exemplo(lote))
            self.assertFalse((raiz / "saida/producao").exists())

    def test_cli_preserva_chave_original_de_ce_df_sem_inventa_la_no_xml_gnre(self):
        for uf, comando in (("CE", "executar"), ("DF", "enviar")):
            with self.subTest(uf=uf), tempfile.TemporaryDirectory() as pasta:
                raiz = Path(pasta)
                conteudo = preparar_lote_uf(pasta, uf)
                lote = raiz / "lote.xml"
                lote.write_bytes(conteudo)
                arquivo = raiz / ("nota-" + uf + ".xml")
                servico = ServicoFalso([recepcao()])
                argumentos = ["emitir_gnre.py", "--dados", str(raiz / "saida"), comando, "--aguardar", "0"]
                if comando == "executar":
                    argumentos += ["--xml", str(arquivo), "--vencimento", "2026-11-16", "--pagamento", "2026-11-16"]
                else:
                    argumentos += ["--lote", str(lote), "--xml-origem", str(arquivo)]
                with patch.object(sys, "argv", argumentos), patch.object(emitir, "ServicoGnre", return_value=servico), patch.object(
                    servico, "configuracao", return_value=configuracao_uf_nova(uf)
                ):
                    self.assertEqual(emitir.main(), 2)
                with closing(emitir.abrir_registro(raiz / "saida")) as con:
                    registro = con.execute("SELECT origens,xml,recibo FROM lotes").fetchone()
                    self.assertIn(CHAVE, registro["origens"])
                    self.assertNotIn(CHAVE.encode(), registro["xml"])
                    self.assertEqual(registro["recibo"], RECIBO)

    def test_envios_concorrentes_so_transmitem_uma_vez(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as primeiro, closing(
            emitir.abrir_registro(Path(pasta))
        ) as segundo:
            lote = emitir.registrar_lote(primeiro, Path(pasta), "homologacao", preparar_lote(pasta))
            servico = ServicoFalso([])
            def durante_envio(tipo, dados):
                outro = segundo.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
                with self.assertRaises(RuntimeError):
                    emitir.receber(segundo, outro, ServicoFalso([]))
                return recepcao()
            with patch.object(servico, "chamar", side_effect=durante_envio) as chamada:
                emitir.receber(primeiro, lote, servico)
            self.assertEqual(chamada.call_count, 1)

    def test_pendencia_consulta_o_mesmo_recibo_ate_obter_pdf(self):
        with tempfile.TemporaryDirectory() as pasta, closing(emitir.abrir_registro(Path(pasta))) as registro:
            conteudo = preparar_lote(pasta)
            lote = emitir.registrar_lote(registro, Path(pasta), "homologacao", conteudo)
            servico = ServicoFalso([
                recepcao(), resultado(conteudo, codigo="401"),
                resultado(conteudo, codigo="403", status="4"),
                resultado(conteudo, pdf=pdf_exemplo(conteudo)),
            ])
            emitir.receber(registro, lote, servico)
            for esperado in (False, False, True):
                registro.execute("UPDATE lotes SET proxima_consulta=0 WHERE id=?", (lote["id"],))
                registro.commit()
                atual = registro.execute("SELECT * FROM lotes WHERE id=?", (lote["id"],)).fetchone()
                self.assertEqual(emitir.consultar_lote(registro, atual, servico), esperado)
            self.assertEqual(servico.chamadas, ["recepcao", "resultado", "resultado", "resultado"])


if __name__ == "__main__":
    unittest.main()
