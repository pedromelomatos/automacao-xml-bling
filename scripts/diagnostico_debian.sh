#!/usr/bin/env bash
# Somente leitura. Executar no notebook Debian, como o usuario da automacao.
set -u

mostrar() {
    printf '\n=== %s ===\n' "$1"
    shift
    "$@" 2>&1 || true
}

mostrar 'Sistema' cat /etc/os-release
mostrar 'Arquitetura' uname -m
mostrar 'Horario' timedatectl show -p Timezone -p NTPSynchronized
mostrar 'Memoria' free -h
mostrar 'Disco' df -h /
mostrar 'Docker cliente' docker --version
mostrar 'Docker Compose' docker compose version
mostrar 'Docker servidor e acesso do usuario' docker info --format '{{.ServerVersion}}'
mostrar 'Docker inicia no boot' systemctl is-enabled docker
mostrar 'Docker ativo' systemctl is-active docker
mostrar 'Containers atuais' docker ps --format '{{.Names}}\t{{.Status}}'
mostrar 'Unidades de suspensao' systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target
printf '\n=== Politicas explicitas de tampa e inatividade ===\n'
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze cat-config systemd/logind.conf 2>/dev/null |
        grep -E '^([[:space:]]*\[Login\]|[[:space:]]*(HandleLidSwitch|HandleLidSwitchExternalPower|HandleLidSwitchDocked|IdleAction|IdleActionSec)[[:space:]]*=)' || true
fi
printf '\nDiagnostico finalizado. Nenhuma configuracao ou servico foi alterado.\n'
