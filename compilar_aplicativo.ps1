param(
    [switch]$IncluirConfiguracaoLocal
)

$ErrorActionPreference = "Stop"
$Raiz = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$PastaEntrega = Join-Path $Raiz "dist\Entrega-Automacao-XML-Bling"

$Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $Launcher) {
    throw "O inicializador do Python (py.exe) não foi encontrado."
}

& $Launcher.Source -3.14 -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller não encontrado. Instale com: py -3.14 -m pip install pyinstaller"
}

Push-Location $Raiz
try {
    & $Launcher.Source -3.14 -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "Automacao-XML-Bling" `
        "app_desktop.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Não foi possível gerar o executável."
    }

    New-Item -ItemType Directory -Path $PastaEntrega -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Raiz "dist\Automacao-XML-Bling.exe") -Destination $PastaEntrega -Force
    Copy-Item -LiteralPath (Join-Path $Raiz ".env.example") -Destination (Join-Path $PastaEntrega ".env.example") -Force
    Copy-Item -LiteralPath (Join-Path $Raiz "configuracao.example.json") -Destination (Join-Path $PastaEntrega "configuracao.example.json") -Force
    Copy-Item -LiteralPath (Join-Path $Raiz "INSTRUCOES_APLICATIVO.txt") -Destination $PastaEntrega -Force

    if ($IncluirConfiguracaoLocal) {
        $EnvLocal = Join-Path $Raiz ".env"
        $ConfiguracaoLocal = Join-Path $Raiz "configuracao.json"
        if (-not (Test-Path -LiteralPath $EnvLocal -PathType Leaf)) {
            throw "O arquivo .env local não foi encontrado."
        }
        if (-not (Test-Path -LiteralPath $ConfiguracaoLocal -PathType Leaf)) {
            throw "O arquivo configuracao.json local não foi encontrado."
        }
        Copy-Item -LiteralPath $EnvLocal -Destination (Join-Path $PastaEntrega ".env") -Force
        Copy-Item -LiteralPath $ConfiguracaoLocal -Destination (Join-Path $PastaEntrega "configuracao.json") -Force
    }

    Write-Host "Aplicativo criado em: $PastaEntrega" -ForegroundColor Green
    Write-Host "O arquivo tokens.json nunca é incluído no pacote." -ForegroundColor Yellow
}
finally {
    Pop-Location
}
