# Oubliez-moi

**Une alternative auto-hébergée à Incogni.** Oubliez-moi envoie en votre nom des demandes d'effacement RGPD aux courtiers en données, suit leurs réponses, relance quand ils ne répondent pas et recommence périodiquement, le tout depuis votre propre serveur et votre propre boîte mail.

![Tableau de bord](docs/dashboard.png)

## Ce que fait Oubliez-moi

| | |
|---|---|
| **Catalogue de ~370 courtiers** | Courtiers d'adresses, plateformes publicitaires et DMP, bureaux de crédit, bases de prospection B2B… issus de la base ouverte [Datenanfragen.de](https://github.com/datenanfragen/data) (CC0). Environ 200 sont activés par défaut pour un résident français (courtiers français et internationaux). Mise à jour automatique tous les 30 jours. |
| **Demandes juridiquement solides** | Effacement (art. 17), opposition à la prospection (art. 21), notification aux destinataires (art. 19) et délai d'un mois (art. 12.3). En français pour les courtiers français, en anglais sinon. |
| **Envoi lissé** | 30 e-mails par heure par défaut, pour ne pas être considéré comme du spam par votre fournisseur. |
| **Relève automatique des réponses** | Via IMAP en lecture seule. Chaque e-mail porte une référence unique (`OUBLI-XXXXXX`) qui permet de rattacher les réponses. Elles sont classées automatiquement (données supprimées, aucune donnée, justificatif demandé, accusé de réception, non-distribution) en français, anglais et allemand. |
| **Relances et CNIL** | Une relance part automatiquement à l'expiration du délai légal. Sans réponse 15 jours plus tard, la demande passe « sans réponse » et Oubliez-moi prépare le texte de la plainte à la CNIL. |
| **Renvoi périodique** | Comme Incogni : 90 jours après une suppression confirmée, la demande est renvoyée, parce que les courtiers recollectent les données. |
| **Plusieurs profils** | Pour vous et vos proches (avec leur accord). |
| **Page « À traiter »** | Les réponses à examiner, les actions requises, les délais dépassés et les échecs, dans une boîte de traitement : liste à gauche, message à droite, une décision en un clic (ou une touche du clavier) et passage automatique au suivant. |
| **Lecture des e-mails** | Les réponses s'affichent avec leur mise en forme d'origine, nettoyées et isolées : scripts, formulaires, images et contenus distants sont bloqués, les pixels de pistage ne peuvent donc pas signaler que vous avez lu le message. Le message cité est replié. |
| **Aide aux formulaires** | Pour les courtiers qui n'acceptent que leur formulaire en ligne : chaque information prête à copier en un clic (nom, prénom, e-mails, téléphones aux deux formats, adresse découpée), message de demande en français et en anglais, et délai d'un mois qui démarre quand vous confirmez l'envoi. |
| **Compléments** | Vous ajoutez une adresse e-mail, un téléphone ou une ancienne adresse au profil après l'envoi ? Oubliez-moi repère les courtiers qui ne l'ont pas reçue et leur envoie un complément sous la même référence. |
| **Protection de vos comptes** | Les organismes chez qui vous avez peut-être un vrai compte (instituts de panels, outils de prospection professionnels, cashback et affiliation, avis clients) sont signalés avant l'envoi, pour que vous puissiez les exclure. |
| **Export CSV** | L'historique complet, utile comme preuve. |

<p align="center"><img src="docs/a-traiter.png" width="49%"> <img src="docs/demande.png" width="49%"></p>

## Installation (Ubuntu + Docker)

Prérequis : un serveur avec [Docker Engine et le plugin Compose](https://docs.docker.com/engine/install/ubuntu/).

```bash
git clone https://github.com/oliv2776/Oubliez-moi.git
cd Oubliez-moi
cp .env.example .env
nano .env            # au minimum : ADMIN_PASSWORD
docker compose up -d --build
docker compose logs -f
```

Par défaut, l'interface écoute sur `http://127.0.0.1:27480`, accessible uniquement depuis le serveur lui-même.

**Accès depuis le réseau local** : mettez `BIND_ADDRESS=0.0.0.0` (ou l'IP du serveur) dans `.env`, puis `docker compose up -d`. L'interface est alors joignable sur `http://IP-DU-SERVEUR:27480`. Si le pare-feu UFW est actif, sachez que Docker publie ses ports sans passer par lui : n'exposez pas ce port sur une IP publique, et limitez l'accès au réseau local ou au VPN.

**Accès distant** : utilisez un reverse proxy HTTPS (voir plus bas) ou un tunnel SSH :

```bash
ssh -L 27480:127.0.0.1:27480 utilisateur@mon-serveur   # puis ouvrez http://localhost:27480
```

### Image prête à l'emploi

Le workflow GitHub Actions construit et publie l'image (amd64 et arm64) sur `ghcr.io/oliv2776/oubliez-moi` à chaque push sur `main`. Pour l'utiliser sans compiler sur le serveur, remplacez `build: .` par `image: ghcr.io/oliv2776/oubliez-moi:latest` dans `docker-compose.yml`. Si le dépôt est privé, rendez le paquet public dans les réglages GitHub ou faites un `docker login ghcr.io`.

## Configuration de la messagerie

Tout se fait dans l'interface, page **Réglages** : adresse e-mail, mot de passe, serveurs d'envoi (SMTP) et de relève (IMAP). Des préréglages remplissent les serveurs pour Gmail, iCloud, OVH, Orange, Free et La Poste. Le bouton **Envoyer un e-mail de test** vérifie la configuration.

Utilisez de préférence **une adresse dédiée** (par exemple `rgpd.prenom@…`). Les courtiers y répondront, et votre boîte principale ne sera pas noyée.

- **Gmail** : il faut un [mot de passe d'application](https://myaccount.google.com/apppasswords) (validation en deux étapes activée). Le mot de passe habituel est refusé.
- **iCloud** : il faut un mot de passe pour app, à créer sur account.apple.com.
- **Outlook.com / Hotmail** : Microsoft impose désormais une connexion OAuth aux logiciels tiers, que l'outil ne gère pas. Préférez une autre adresse.
- Ajoutez le dossier spam aux dossiers surveillés (Gmail : `INBOX, [Gmail]/Spam`). Les réponses de courtiers y finissent souvent.

Les réglages saisis dans l'interface sont enregistrés dans la base (volume Docker) et prennent le pas sur le fichier `.env`.

## Premier lancement

1. Connectez-vous avec `ADMIN_USER` / `ADMIN_PASSWORD`.
2. Dans **Réglages**, configurez votre adresse e-mail, enregistrez, puis envoyez un e-mail de test.
3. Créez votre profil (nom, toutes vos adresses e-mail, téléphones, adresses postales actuelles et anciennes).
4. Cliquez sur **Aperçu du courrier** pour relire la lettre.
5. Dans **Réglages**, passez le mode d'envoi de « Simulation » à « Réel ».
6. Sur la page du profil, cliquez sur **Lancer les demandes**.

Vous pouvez aussi lancer les demandes en simulation pour voir le déroulé : rien ne part. Au passage en envoi réel, les demandes simulées sont automatiquement remises en file et envoyées pour de bon.

Ensuite, passez de temps en temps sur **À traiter**. Tout le reste est automatique.

## Reverse proxy HTTPS

**Caddy** (certificat Let's Encrypt automatique), dans `/etc/caddy/Caddyfile` :

```
oubliez-moi.mondomaine.fr {
    reverse_proxy 127.0.0.1:27480
}
```

**Nginx** :

```nginx
server {
    server_name oubliez-moi.mondomaine.fr;
    location / {
        proxy_pass http://127.0.0.1:27480;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    # listen 443 ssl; + certificats (certbot --nginx)
}
```

Mettez ensuite `COOKIE_SECURE=true` dans `.env`. L'application contient des données personnelles sensibles : ne l'exposez jamais en HTTP simple, et envisagez de la réserver à votre VPN (WireGuard, Tailscale…).

## Toutes les options

Voir [`.env.example`](.env.example), qui documente chaque variable. La messagerie se règle dans l'interface ; dans le `.env`, les plus utiles sont :

| Variable | Défaut | Rôle |
|---|---|---|
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / — | Accès à l'interface |
| `BIND_ADDRESS` | `127.0.0.1` | `0.0.0.0` pour un accès depuis le réseau local |
| `BROKER_COUNTRIES` | `fr,all` | Pays dont les courtiers sont activés par défaut (`all` = internationaux) |
| `RESPONSE_DAYS` | `30` | Délai avant relance |
| `REMINDER_WAIT_DAYS` | `15` | Délai après relance avant « sans réponse » |
| `RESEND_INTERVAL_DAYS` | `90` | Renvoi après confirmation (`0` = jamais) |
| `AUTO_ENROLL_NEW_BROKERS` | `true` | Contacter automatiquement les nouveaux courtiers du catalogue |
| `BROKER_AUTO_UPDATE_DAYS` | `30` | Mise à jour du catalogue (`0` = manuelle) |

## Sauvegarde et mise à jour

Toutes les données sont dans le volume Docker `oubliez-moi_oubliez-moi-data` (base SQLite `oubliez-moi.db`).

```bash
# Sauvegarde
docker run --rm -v oubliez-moi_oubliez-moi-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/oubliez-moi-$(date +%F).tar.gz -C /data .

# Mise à jour
git pull && docker compose up -d --build
```

## Limites, à lire

- **Ce n'est pas un conseil juridique.** Les courriers s'appuient sur le RGPD, mais chaque situation est différente.
- **Couverture** : le catalogue est fort sur les courtiers européens et l'adtech internationale. Il ne couvre pas les sites américains de recherche de personnes (Spokeo, Whitepages…), qui ne concernent guère les résidents européens et n'acceptent en général que leurs propres formulaires. Vous pouvez ajouter n'importe quel organisme depuis **Courtiers → Ajouter**.
- **Pièces d'identité** : Oubliez-moi n'en envoie jamais. Le RGPD (art. 12.6) n'en autorise l'exigence qu'en cas de doute raisonnable. Si un courtier insiste, la demande passe en « Action requise » ; vous décidez alors quoi lui transmettre (de préférence une copie masquée).
- **Comptes chez les marchands** : Oubliez-moi n'écrit qu'aux organismes du catalogue (courtiers en données, régies et plateformes publicitaires, bureaux de crédit) et jamais aux sites où vous achetez, sauf si vous les ajoutez vous-même. Une demande vise uniquement l'organisme destinataire : effacer vos données chez une régie publicitaire ne touche pas votre compte chez un marchand qui travaille avec elle. Les grandes plateformes (Google, Meta, Amazon, Microsoft…) sont exclues du catalogue, et les organismes où l'on peut aussi avoir un compte sont signalés avant l'envoi.
- **Classement automatique** : il repose sur des mots-clés et peut se tromper. Chaque décision est tracée dans l'historique avec l'expression qui l'a déclenchée, et vous pouvez la corriger en un clic.
- **Proches** : n'envoyez de demande au nom d'une autre personne qu'avec son accord.
- **Sécurité** : la base contient les informations qui vous identifient. Mot de passe fort, HTTPS, disque chiffré et sauvegardes sont recommandés.

## Développement

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q

# Lancer en local (simulation, données dans ./data)
DATA_DIR=./data ADMIN_PASSWORD=dev uvicorn app.main:app --reload --port 27480
```

Structure :

```
app/
  main.py        routes web (FastAPI + Jinja2)
  service.py     campagnes, envoi, relances, rattachement des réponses
  mailer.py      SMTP, IMAP, analyse et classement des réponses
  letters.py     modèles de courriers (FR/EN) et texte de plainte CNIL
  brokers.py     import du catalogue Datenanfragen
  scheduler.py   tâches périodiques (APScheduler)
  db.py          modèle SQLite (SQLAlchemy)
  seed/          catalogue embarqué (généré depuis Datenanfragen)
tests/           tests pytest
```

Pour régénérer le catalogue embarqué depuis un clone de `datenanfragen/data` :

```bash
python -m app.brokers /chemin/vers/data/companies app/seed/brokers.json
```

## Crédits et licence

- Code : licence [MIT](LICENSE).
- Catalogue des courtiers : [Datenanfragen.de e. V.](https://www.datarequests.org/), base `datenanfragen/data` sous licence [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). Si une adresse est obsolète, corrigez-la chez eux : tout le monde en profite.
- Oubliez-moi n'est affilié ni à Incogni ni à Surfshark.
