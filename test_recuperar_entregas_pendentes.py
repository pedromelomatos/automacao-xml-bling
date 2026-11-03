"""Recuperação de entregas com arquivos sintéticos; sem API, Redis ou Dropbox reais."""

from datetime import date
import json
from pathlib import Path
from unittest.mock import patch
import unittest

import integracao_dropbox as integracao
import recuperar_entregas_pendentes as recuperacao
from test_integracao_dropbox import BaseIntegracao


class RecuperacaoTests(BaseIntegracao):
    def preparar(self, numero, situacao="5", remover_metadados=True):
        row, nota = self.nova_nota(numero, situacao=situacao)
        nota.update(numero=str(numero), dataEmissao="2026-10-30 08:00:00")
        outbox = self.dados / "entregas/metadados" / f"{row['chave']}.json"
        if remover_metadados:
            outbox.unlink()
        return row, nota, outbox

    def recuperar(self, notas, aplicar=True):
        with patch.object(recuperacao, "ClienteBling", side_effect=AssertionError("Sem rede")):
            return recuperacao.recuperar(date(2026, 10, 30), notas=notas,
                                       aplicar=aplicar, cfg=self.cfg(), root=self.dados)

    def test_recupera_danfe_local_preserva_existente_e_repeticao_nao_duplica(self):
        row, nota, outbox = self.preparar(9901)
        _, existente, anterior = self.preparar(9902, "6", remover_metadados=False)
        bytes_antes = anterior.read_bytes()
        report = self.recuperar([nota, existente])
        self.assertEqual((report["novos_metadados"], report["metadados_existentes"],
                          report["metadados_registrados"]), (1, 1, 1))
        self.assertEqual(json.loads(outbox.read_text())["situacao_bling"], "5")
        self.assertEqual(anterior.read_bytes(), bytes_antes)
        self.assertEqual(self.recuperar([nota, existente])["metadados_registrados"], 0)
        self.assertEqual(integracao.planejar(self.cfg(), integracao.carregar_metadados(self.dados), {})["resumo"],
                         {"enviar": 2})
        self.assertEqual((report["downloads_executados"], report["uploads_executados"]), (0, 0))

    def test_simulacao_nao_registra_nota_e_cancelada_nao_entra(self):
        _, nota, outbox = self.preparar(9911)
        _, cancelada, _ = self.preparar(9912, "2")
        report = self.recuperar([nota, cancelada], aplicar=False)
        self.assertEqual((report["novos_metadados"], report["metadados_registrados"],
                          report["fora_da_janela_ou_situacao"]), (1, 0, 1))
        self.assertFalse(outbox.exists())

    def test_xml_ausente_bloqueia_conjunto_sem_baixar_ou_registrar_parcial(self):
        _, valida, outbox = self.preparar(9921)
        row, ausente, _ = self.preparar(9922)
        Path(row["caminho_xml"]).unlink()
        report = self.recuperar([valida, ausente])
        self.assertEqual(report["estado"], "bloqueada")
        self.assertEqual(report["metadados_registrados"], 0)
        self.assertFalse(outbox.exists())

    def test_xml_sem_protocolo_autorizado_bloqueia_situacao5(self):
        row, nota, outbox = self.preparar(9931)
        path = Path(row["caminho_xml"])
        path.write_bytes(path.read_bytes().replace(b"<cStat>100</cStat>", b"<cStat>101</cStat>"))
        report = self.recuperar([nota])
        self.assertEqual(report["estado"], "bloqueada")
        self.assertFalse(outbox.exists())

    def test_canal_divergente_nao_substitui_metadados(self):
        _, nota, outbox = self.preparar(9941, remover_metadados=False)
        antes = outbox.read_bytes()
        nota["loja"]["id"] = "99999"
        self.assertEqual(self.recuperar([nota])["estado"], "bloqueada")
        self.assertEqual(outbox.read_bytes(), antes)


if __name__ == "__main__":
    unittest.main()
