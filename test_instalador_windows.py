"""Exercita o instalador em um processo isolado, sem API ou tarefas reais."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
POWERSHELL = shutil.which("powershell.exe")

PYTHON_FALSO = r'''
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Argumentos)
$global:LASTEXITCODE = 0
if ($Argumentos -contains "--version") { Write-Output "Python 3.14.7"; return }
if ($Argumentos[0] -like "*preparar_instalacao.py") {
    Add-Content -LiteralPath $env:TESTE_RASTRO -Value ("preparar " + ($Argumentos -join " "))
    $global:LASTEXITCODE = [int]$env:TESTE_FALHA_PREPARACAO
}
if ($Argumentos[0] -like "*executar_diario.py") {
    Add-Content -LiteralPath $env:TESTE_RASTRO -Value "simular"
}
'''

HARNESS = r'''
param([string]$Instalador, [string]$Opcao)
function Get-Command {
    param([string]$Name, [string]$ErrorAction)
    if ($Name -eq "python.exe") { return [PSCustomObject]@{ Source = $env:TESTE_PYTHON } }
    if ($Name -eq "py.exe") { return $null }
    Microsoft.PowerShell.Core\Get-Command -Name $Name -ErrorAction $ErrorAction
}
function Get-FileHash {
    param([string]$LiteralPath, [string]$Algorithm)
    [PSCustomObject]@{ Hash = "9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649" }
}
function Import-Module { param([string]$Name, [string]$ErrorAction) }
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory); return "acao" }
function New-ScheduledTaskTrigger { param([switch]$Daily, $At); return "gatilho" }
function New-ScheduledTaskSettingsSet {
    param([switch]$StartWhenAvailable, [switch]$WakeToRun, [switch]$AllowStartIfOnBatteries,
          [switch]$DontStopIfGoingOnBatteries, $MultipleInstances, $RestartCount,
          $RestartInterval, $ExecutionTimeLimit)
    return "configuracoes"
}
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel); return "usuario" }
function Register-ScheduledTask {
    param($TaskName, $Description, $Action, $Trigger, $Settings, $Principal, [switch]$Force)
    Add-Content -LiteralPath $env:TESTE_RASTRO -Value "agendar"
}
function Get-ScheduledTaskInfo { param($TaskName); [PSCustomObject]@{ NextRunTime = "teste" } }
if ($Opcao -eq "validar") { & $Instalador -ValidarSomente }
else { & $Instalador -PrimeiraData "2026-10-01" }
exit $LASTEXITCODE
'''


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Requer Windows PowerShell")
class InstaladorWindowsTests(unittest.TestCase):
    def executar(self, falha=0, opcao="instalar"):
        with tempfile.TemporaryDirectory(prefix="bling-instalador-") as pasta:
            raiz = Path(pasta)
            instalador = raiz / "instalar_automacao.ps1"
            shutil.copy2(BASE_DIR / instalador.name, instalador)
            for nome in ("executar_diario.py", "preparar_instalacao.py", "baixar_xmls.py",
                         "organizar_xmls.py", "bling_auth.py", "configuracao.json",
                         "requirements.txt", ".env", "tokens.json", "python-3.14.7-amd64.exe"):
                (raiz / nome).write_text("fixture", encoding="utf-8")
            (raiz / "dependencias").mkdir()
            (raiz / "dependencias" / "fixture.whl").write_bytes(b"wheel-teste")
            python_falso = raiz / "python-falso.ps1"
            python_falso.write_text(PYTHON_FALSO, encoding="utf-8-sig")
            harness = raiz / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8-sig")
            rastro = raiz / "rastro.txt"
            ambiente = dict(os.environ, TESTE_RASTRO=str(rastro),
                            TESTE_PYTHON=str(python_falso), TESTE_FALHA_PREPARACAO=str(falha))
            resultado = subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(harness), "-Instalador", str(instalador), "-Opcao", opcao],
                env=ambiente, capture_output=True, timeout=30,
            )
            eventos = rastro.read_text(encoding="utf-8-sig").splitlines() if rastro.exists() else []
            return resultado, eventos

    def test_falha_do_bling_impede_simulacao_e_agendamento(self):
        resultado, eventos = self.executar(falha=1)
        self.assertEqual(resultado.returncode, 1, resultado.stdout.decode(errors="replace"))
        self.assertEqual(len(eventos), 1)
        self.assertIn("--primeira-data 2026-10-01", eventos[0])

    def test_sucesso_valida_e_simula_antes_de_agendar(self):
        resultado, eventos = self.executar()
        self.assertEqual(resultado.returncode, 0, resultado.stdout.decode(errors="replace"))
        self.assertIn("--primeira-data 2026-10-01", eventos[0])
        self.assertEqual(eventos[1:], ["simular", "agendar"])

    def test_validar_somente_nao_agenda(self):
        resultado, eventos = self.executar(opcao="validar")
        self.assertEqual(resultado.returncode, 0, resultado.stdout.decode(errors="replace"))
        self.assertIn("--validar-somente", eventos[0])
        self.assertEqual(eventos[1:], ["simular"])


if __name__ == "__main__":
    unittest.main()
