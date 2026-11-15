#!/bin/sh
# Installe la mise à jour automatique d'Oubliez-moi : chaque nuit, deploy/update.sh récupère les images
# publiées (correctifs de sécurité compris) et redémarre les conteneurs qui ont changé.
#     sudo deploy/install-auto-update.sh            installer
#     sudo deploy/install-auto-update.sh --remove   désinstaller
set -eu

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
DIR=$(cd "$(dirname "$0")/.." && pwd)
UNIT=/etc/systemd/system/oubliez-moi-update

if [ "${1:-}" = "--remove" ]; then
    systemctl disable --now oubliez-moi-update.timer 2>/dev/null || true
    rm -f "$UNIT.service" "$UNIT.timer"
    systemctl daemon-reload
    echo "Mise à jour automatique désinstallée."
    exit 0
fi

chmod +x "$DIR/deploy/update.sh"
cat > "$UNIT.service" <<UNIT
[Unit]
Description=Mise à jour d'Oubliez-moi (images Docker publiées)
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=$DIR/deploy/update.sh
UNIT

cat > "$UNIT.timer" <<UNIT
[Unit]
Description=Mise à jour nocturne d'Oubliez-moi

[Timer]
# Chaque nuit vers 5 h, à une minute tirée au hasard dans l'heure ; rattrapée au réveil si le serveur dormait.
OnCalendar=*-*-* 05:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now oubliez-moi-update.timer
echo "Mise à jour automatique installée pour $DIR."
echo "Prochaine exécution : $(systemctl list-timers oubliez-moi-update.timer --no-legend | awk '{print $1, $2, $3}')"
echo "Journal : journalctl -u oubliez-moi-update"
