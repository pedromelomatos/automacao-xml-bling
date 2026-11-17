<#
.SYNOPSIS
Envia XMLs de NF-e para homologacao GNRE e baixa os resultados do Debian.
.EXAMPLE
.\homologar.ps1 "C:\Notas para teste"
.EXAMPLE
.\homologar.ps1 "C:\Notas para teste" -Simular
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Pasta,
    [switch]$Simular
)

$ErrorActionPreference = 'Stop'
$configSsh = Join-Path $PSScriptRoot 'segredos\ssh\config'
$executor = Join-Path $PSScriptRoot 'scripts\homologar_pasta_gnre.py'
if (!(Test-Path -LiteralPath $configSsh -PathType Leaf)) {
    throw 'Configuracao SSH nao encontrada em segredos\ssh\config.'
}
foreach ($programa in @('ssh', 'scp')) {
    if (!(Get-Command $programa -ErrorAction SilentlyContinue)) {
        throw "Comando $programa nao encontrado. Instale o cliente OpenSSH do Windows."
    }
}

$origem = Get-Item -LiteralPath $Pasta
if ($origem.PSIsContainer) {
    $arquivos = @(Get-ChildItem -LiteralPath $origem.FullName -File |
        Where-Object { $_.Extension -ieq '.xml' } | Sort-Object Name)
    $pastaResultados = $origem.FullName
} elseif ($origem.Extension -ieq '.xml') {
    $arquivos = @($origem)
    $pastaResultados = $origem.DirectoryName
} else {
    throw 'Informe uma pasta contendo XMLs ou o caminho de um XML de NF-e.'
}
if ($arquivos.Count -eq 0) { throw 'Nenhum XML encontrado nessa pasta.' }

# O destino usa apenas caracteres gerados aqui; nomes locais nao viram comandos.
$sessao = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
$remoto = "/home/e-c/gnre-emissao/entrada/homologacao-$sessao"
& ssh -F $configSsh debian-automacao "umask 077; mkdir -p $remoto"
if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel criar a pasta de entrada no Debian.' }

Write-Host "Enviando $($arquivos.Count) XML(s) ao Debian..."
$indice = 0
$mapa = @{}
foreach ($arquivo in $arquivos) {
    $indice++
    $nome = '{0:D4}.xml' -f $indice
    $mapa[$nome] = $arquivo.Name
    & scp -q -F $configSsh -- $arquivo.FullName "debian-automacao:$remoto/$nome"
    if ($LASTEXITCODE -ne 0) { throw "Falha ao copiar $($arquivo.Name). Nenhuma emissao foi iniciada." }
}
& scp -q -F $configSsh -- $executor "debian-automacao:$remoto/homologar_pasta_gnre.py"
if ($LASTEXITCODE -ne 0) { throw 'Falha ao copiar o executor. Nenhuma emissao foi iniciada.' }

$comando = "python3 -u $remoto/homologar_pasta_gnre.py $remoto"
if ($Simular) { $comando += ' --simular' }
& ssh -F $configSsh debian-automacao $comando
$codigo = $LASTEXITCODE
if ($codigo -notin @(0, 1, 2)) {
    throw 'Conexao ou executor interrompido. Os registros foram preservados; nao force um reenvio.'
}

$saida = Join-Path $pastaResultados "Resultados-homologacao-$sessao"
New-Item -ItemType Directory -Path $saida | Out-Null
& scp -q -r -F $configSsh "debian-automacao:$remoto/resultados/." $saida
if ($LASTEXITCODE -ne 0) {
    throw "Nao foi possivel baixar os resultados. Eles continuam em $remoto/resultados no Debian."
}
$mapa | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $saida 'arquivos-originais.json') -Encoding UTF8
Write-Host "Resultados salvos em: $saida"
if ($Simular) { Write-Host 'Simulacao concluida: nenhuma guia transmitida ou consultada.' }
if ($codigo -eq 1) { Write-Warning 'Ha XMLs com erros ou rejeicoes. Consulte resumo.json na pasta de resultados.' }
if ($codigo -eq 2) { Write-Warning 'Ha lotes pendentes. Repita o mesmo comando para consultar os recibos existentes.' }
exit $codigo
