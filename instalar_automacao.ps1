param(
    [switch]$ValidarSomente
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$NomeTarefa = "Automacao XML Bling - Diario"
$PastaInstalador = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$RaizPossivel = Split-Path -Parent $PastaInstalador
$PastaOrganizada = Join-Path $RaizPossivel "arquivos-automacao"
if (Test-Path -LiteralPath $PastaOrganizada -PathType Container) {
    $PastaProjeto = (Resolve-Path -LiteralPath $PastaOrganizada).Path
}
else {
    $PastaProjeto = $PastaInstalador
}
$Executor = Join-Path $PastaProjeto "executar_diario.py"
$Requisitos = Join-Path $PastaProjeto "requirements.txt"
$Dependencias = Join-Path $PastaInstalador "dependencias"
$InstaladorPython = Join-Path $PastaInstalador "python-3.14.7-amd64.exe"
$HashInstaladorPython = "9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649"

function Escrever-Etapa([string]$Mensagem) {
    Write-Host "[OK] $Mensagem" -ForegroundColor Green
}

function Testar-Python([string]$Executavel, [string[]]$Prefixo) {
    if (-not $Executavel -or -not (Test-Path -LiteralPath $Executavel -PathType Leaf)) {
        return $null
    }
    try {
        $Argumentos = @($Prefixo) + @("--version")
        $Saida = & $Executavel @Argumentos 2>&1
        if ($LASTEXITCODE -eq 0 -and "$Saida" -match '^Python 3\.14\.') {
            return [PSCustomObject]@{
                Executavel = $Executavel
                Prefixo = @($Prefixo)
                Versao = "$($Saida | Select-Object -First 1)"
            }
        }
    }
    catch {
        return $null
    }
    return $null
}

function Encontrar-Python {
    $Candidatos = @()
    $ComandoPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($ComandoPython) {
        $Candidatos += ,@($ComandoPython.Source, @())
    }
    $ComandoPy = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($ComandoPy) {
        $Candidatos += ,@($ComandoPy.Source, @("-3.14"))
    }
    $Candidatos += ,@(
        (Join-Path $env:LocalAppData "Programs\Python\Python314\python.exe"),
        @()
    )
    $Candidatos += ,@(
        (Join-Path $env:LocalAppData "Python\pythoncore-3.14-64\python.exe"),
        @()
    )

    foreach ($Candidato in $Candidatos) {
        $Resultado = Testar-Python $Candidato[0] $Candidato[1]
        if ($Resultado) {
            return $Resultado
        }
    }
    return $null
}

function Instalar-Python {
    if (Test-Path -LiteralPath $InstaladorPython -PathType Leaf) {
        $HashAtual = (Get-FileHash -LiteralPath $InstaladorPython -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($HashAtual -ne $HashInstaladorPython) {
            throw "O instalador local do Python não passou na verificação SHA256."
        }

        Write-Host "Python 3.14 não encontrado. Instalando a cópia incluída no pacote..." -ForegroundColor Yellow
        $ArgumentosInstalador = @(
            "/quiet",
            "InstallAllUsers=0",
            "PrependPath=1",
            "Include_pip=1",
            "Include_launcher=1",
            "InstallLauncherAllUsers=0",
            "Include_test=0",
            "Shortcuts=0"
        )
        $Processo = Start-Process `
            -FilePath $InstaladorPython `
            -ArgumentList $ArgumentosInstalador `
            -Wait `
            -PassThru `
            -WindowStyle Hidden
        if ($Processo.ExitCode -notin @(0, 3010)) {
            throw "O instalador local do Python terminou com o código $($Processo.ExitCode)."
        }
    }
    else {
        $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $Winget) {
            throw "Python 3.14, instalador local e winget não foram encontrados."
        }

        Write-Host "Instalador local ausente. Instalando Python 3.14 pelo winget..." -ForegroundColor Yellow
        $ArgumentosWinget = @(
            "install",
            "--id", "Python.Python.3.14",
            "--exact",
            "--scope", "user",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity"
        )
        & $Winget.Source @ArgumentosWinget | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "O winget não conseguiu instalar o Python 3.14."
        }
    }

    $CaminhoMaquina = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $CaminhoUsuario = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$CaminhoMaquina;$CaminhoUsuario"
    $Resultado = Encontrar-Python
    if (-not $Resultado) {
        throw "O Python foi instalado, mas não pôde ser localizado. Reinicie o computador e abra o instalador novamente."
    }
    return $Resultado
}

try {
    Write-Host "Configurando a Automação XML Bling..." -ForegroundColor Cyan
    Write-Host "Pasta: $PastaProjeto"

    $ArquivosObrigatorios = @(
        "executar_diario.py",
        "baixar_xmls.py",
        "organizar_xmls.py",
        "bling_auth.py",
        "configuracao.json",
        "requirements.txt",
        ".env",
        "tokens.json"
    )
    $Ausentes = @(
        $ArquivosObrigatorios | Where-Object {
            -not (Test-Path -LiteralPath (Join-Path $PastaProjeto $_) -PathType Leaf)
        }
    )
    if ($Ausentes.Count -gt 0) {
        throw "Arquivos ausentes: $($Ausentes -join ', ')."
    }
    if (-not (Test-Path -LiteralPath $InstaladorPython -PathType Leaf)) {
        throw "Instalador Python ausente: python-3.14.7-amd64.exe."
    }
    if (-not (Test-Path -LiteralPath $Dependencias -PathType Container)) {
        throw "Pasta de dependências ausente: dependencias."
    }
    $Wheels = @(Get-ChildItem -LiteralPath $Dependencias -File -Filter "*.whl")
    if ($Wheels.Count -eq 0) {
        throw "Nenhuma dependência offline foi encontrada na pasta dependencias."
    }
    $HashAtual = (Get-FileHash -LiteralPath $InstaladorPython -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($HashAtual -ne $HashInstaladorPython) {
        throw "O instalador local do Python não passou na verificação SHA256."
    }
    Escrever-Etapa "Arquivos necessários encontrados."
    Escrever-Etapa "Instalador oficial do Python verificado e $($Wheels.Count) dependências offline encontradas."

    $PythonEncontrado = Encontrar-Python
    if (-not $PythonEncontrado) {
        if ($ValidarSomente) {
            throw "Python 3.14 não encontrado. No modo normal ele será instalado pelo pacote local."
        }
        $PythonEncontrado = Instalar-Python
    }
    $PythonExe = $PythonEncontrado.Executavel
    $PrefixoPython = @($PythonEncontrado.Prefixo)
    Escrever-Etapa "$($PythonEncontrado.Versao) encontrado em $PythonExe."

    $ArgumentosPip = $PrefixoPython + @("-m", "pip", "--version")
    & $PythonExe @ArgumentosPip 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Ativando o pip incluído no Python..." -ForegroundColor Yellow
        $ArgumentosEnsurePip = $PrefixoPython + @("-m", "ensurepip", "--upgrade")
        & $PythonExe @ArgumentosEnsurePip
        if ($LASTEXITCODE -ne 0) {
            throw "Não foi possível ativar o pip."
        }
    }

    $ArgumentosImportacao = $PrefixoPython + @("-c", "import requests, dotenv")
    & $PythonExe @ArgumentosImportacao 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($ValidarSomente) {
            throw "Dependências Python ausentes."
        }
        Write-Host "Instalando dependências Python incluídas no pacote..." -ForegroundColor Yellow
        $ArgumentosPip = $PrefixoPython + @(
            "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links", $Dependencias,
            "-r", $Requisitos
        )
        & $PythonExe @ArgumentosPip
        if ($LASTEXITCODE -ne 0) {
            throw "Falha ao instalar as dependências Python."
        }
    }
    Escrever-Etapa "Dependências Python disponíveis."

    $ArgumentosSimulacao = $PrefixoPython + @($Executor, "--simular")
    & $PythonExe @ArgumentosSimulacao
    if ($LASTEXITCODE -ne 0) {
        throw "A simulação do executor falhou."
    }
    Escrever-Etapa "Simulação do executor concluída."

    if ($ValidarSomente) {
        Write-Host "Validação concluída; nenhuma tarefa foi criada." -ForegroundColor Cyan
        exit 0
    }

    Import-Module ScheduledTasks -ErrorAction Stop
    $ArgumentosTarefa = ($PrefixoPython + @('"' + $Executor + '"')) -join " "
    $Acao = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument $ArgumentosTarefa `
        -WorkingDirectory $PastaProjeto
    $Gatilho = New-ScheduledTaskTrigger -Daily -At "14:10"
    $Configuracoes = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -WakeToRun `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 10) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    $Usuario = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $Usuario `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $NomeTarefa `
        -Description "Baixa e organiza diariamente as NF-e do Bling, com recuperação de dias pendentes." `
        -Action $Acao `
        -Trigger $Gatilho `
        -Settings $Configuracoes `
        -Principal $Principal `
        -Force | Out-Null

    $Informacoes = Get-ScheduledTaskInfo -TaskName $NomeTarefa
    Escrever-Etapa "Tarefa '$NomeTarefa' criada para executar diariamente às 14h10."
    Write-Host "Próxima execução: $($Informacoes.NextRunTime)"
    Write-Host "A tarefa será executada enquanto o usuário $Usuario estiver conectado."
    exit 0
}
catch {
    Write-Host "[ERRO] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
