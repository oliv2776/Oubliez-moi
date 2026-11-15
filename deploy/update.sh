#!/bin/sh
# Met à jour les conteneurs d'Oubliez-moi depuis les images publiées, puis vérifie que l'appli répond.
# Si la nouvelle version ne démarre pas correctement, l'image précédente est remise en service.
# Lancé chaque nuit par le minuteur systemd (deploy/install-auto-update.sh), ou à la main :
#     sudo deploy/update.sh
set -eu

cd "$(dirname "$0")/.."
SERVICE=oubliez-moi
log() { echo "[mise à jour] $*"; }

image=$(docker compose config --images "$SERVICE" | head -n 1)
before=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)

# Les services des profils listés dans COMPOSE_PROFILES (.env), par exemple ia ou proton, sont mis à jour aussi.
docker compose pull --quiet
after=$(docker image inspect --format '{{.Id}}' "$image")
docker compose up -d --remove-orphans

if [ "$before" = "$after" ]; then
    log "Oubliez-moi est déjà à jour."
else
    log "Nouvelle image d'Oubliez-moi : vérification du démarrage…"
    status=starting
    for _ in $(seq 1 36); do  # jusqu'à 3 minutes
        status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$SERVICE" 2>/dev/null || echo absent)
        [ "$status" = healthy ] && break
        sleep 5
    done
    if [ "$status" = healthy ]; then
        log "Nouvelle version en service."
    elif [ -n "$before" ]; then
        log "La nouvelle version ne répond pas ($status) : retour à l'image précédente."
        docker tag "$before" "$image"
        docker compose up -d --force-recreate "$SERVICE"
        exit 1
    else
        log "La nouvelle version ne répond pas ($status) et aucune image précédente n'est disponible."
        exit 1
    fi
fi

# Anciennes images devenues inutiles (celles en service sont conservées).
docker image prune -f >/dev/null
