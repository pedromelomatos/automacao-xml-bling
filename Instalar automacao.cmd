@echo off
chcp 65001 >nul
setlocal

title Instalar Automacao XML Bling
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar_automacao.ps1"
set "resultado=%errorlevel%"

echo.
if not "%resultado%"=="0" (
    echo A instalacao nao foi concluida. Leia a mensagem acima.
) else (
    echo Instalacao concluida.
)
echo.
pause
exit /b %resultado%
