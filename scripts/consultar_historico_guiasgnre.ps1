<# Consulta somente guias autorizadas/pendentes; nao possui operacao de emissao. #>
[CmdletBinding()]
param(
    [string]$Credenciais = (Join-Path $PSScriptRoot '../segredos/gnre.json'),
    [Parameter(Mandatory=$true)][string]$Saida
)
$ErrorActionPreference = 'Stop'
$contasHistorico = Get-Content -Raw -LiteralPath $Credenciais | ConvertFrom-Json
$pastaHistorico = New-Item -ItemType Directory -Path $Saida -Force
$resumoHistorico = @()
foreach ($contaHistorico in $contasHistorico.PSObject.Properties) {
    if ($contaHistorico.Name -notmatch '^[A-Z0-9]{14}$') { throw 'Identificador de conta invalido.' }
    foreach ($operacaoHistorico in @('BuscaTodasGuiasAutorizadas','BuscaTodasGuiasPendentes')) {
        $registroHistorico = [ordered]@{cnpj=$contaHistorico.Name; operacao=$operacaoHistorico; consultado_em=[DateTimeOffset]::UtcNow.ToString('o')}
        $handlerHistorico = [System.Net.Http.HttpClientHandler]::new()
        $handlerHistorico.AllowAutoRedirect = $false
        $clienteHistorico = [System.Net.Http.HttpClient]::new($handlerHistorico)
        $clienteHistorico.Timeout = [TimeSpan]::FromSeconds(45)
        try {
            $xmlHistorico = [xml]'<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body/></soap:Envelope>'
            $metodoHistorico = $xmlHistorico.CreateElement($operacaoHistorico,'http://guiasgnre/webgnre')
            [void]$xmlHistorico.DocumentElement.FirstChild.AppendChild($metodoHistorico)
            $camposHistorico = [ordered]@{sEmpresa=$contaHistorico.Value.empresa; sUsuario=$contaHistorico.Value.usuario; sChaveApi=$contaHistorico.Value.chave_api; sDataInicio=''; sDataFim=''}
            foreach ($campoHistorico in $camposHistorico.GetEnumerator()) {
                if ($campoHistorico.Key -in @('sEmpresa','sUsuario','sChaveApi') -and [string]::IsNullOrWhiteSpace([string]$campoHistorico.Value)) { throw 'Credencial incompleta.' }
                $elementoHistorico=$xmlHistorico.CreateElement($campoHistorico.Key,'http://guiasgnre/webgnre')
                $elementoHistorico.InnerText=[string]$campoHistorico.Value
                [void]$metodoHistorico.AppendChild($elementoHistorico)
            }
            $corpoHistorico = [System.Net.Http.StringContent]::new($xmlHistorico.OuterXml,[Text.Encoding]::UTF8,'application/soap+xml')
            try { $respostaHistorico=$clienteHistorico.PostAsync('https://www.guiasgnre.com.br/webgnre/geraguiagnre.wso',$corpoHistorico).GetAwaiter().GetResult() } finally { $corpoHistorico.Dispose() }
            try {
                if (!$respostaHistorico.IsSuccessStatusCode) { throw 'Consulta HTTP nao concluida.' }
                $bytesHistorico=$respostaHistorico.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
                if ($bytesHistorico.Length -gt 67108864) { throw 'Historico excede o limite de leitura.' }
                $textoHistorico=[Text.Encoding]::UTF8.GetString($bytesHistorico)
                if ($textoHistorico -match '<!DOCTYPE|<!ENTITY') { throw 'XML com declaracoes externas.' }
                [xml]$retornoHistorico=$textoHistorico
                $resultadoHistorico=$retornoHistorico.SelectSingleNode('//*[local-name()="'+$operacaoHistorico+'Result"]')
                if (!$resultadoHistorico) { throw 'Resposta sem resultado esperado.' }
                $codigoHistorico=$resultadoHistorico.SelectSingleNode('*[local-name()="retorno"]').InnerText
                $nomeHistorico=$contaHistorico.Name+'-'+$operacaoHistorico+'.xml'
                [IO.File]::WriteAllBytes((Join-Path $pastaHistorico.FullName $nomeHistorico),$bytesHistorico)
                $registroHistorico['codigo']=$codigoHistorico
                $registroHistorico['guias']=@($resultadoHistorico.SelectNodes('.//*[local-name()="tGuias"]')).Count
                $registroHistorico['arquivo']=$nomeHistorico
                $registroHistorico['sha256']=(Get-FileHash -LiteralPath (Join-Path $pastaHistorico.FullName $nomeHistorico)).Hash.ToLower()
                $registroHistorico['concluida']=($codigoHistorico -in @('0','3'))
            } finally { $respostaHistorico.Dispose() }
        } catch {
            # Nunca mostrar corpo de pedido, credenciais ou mensagens que possam ecoa-las.
            $registroHistorico['concluida']=$false
            $registroHistorico['falha']='Consulta nao concluida ('+$_.Exception.GetType().Name+').'
        } finally { $clienteHistorico.Dispose(); $handlerHistorico.Dispose() }
        $resumoHistorico += [pscustomobject]$registroHistorico
        $resumoHistorico | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $pastaHistorico.FullName 'consultas.json') -Encoding utf8
        [pscustomobject]$registroHistorico | ConvertTo-Json -Compress
    }
}
if (@($resumoHistorico | Where-Object { !$_.concluida }).Count -gt 0) { exit 1 }
