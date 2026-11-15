# Journal des versions

Les versions sont étiquetées dans Git (`v1.24.0`) et publiées en images (`ghcr.io/oliv2776/oubliez-moi:1.24.0`).
Pour revenir à une version : `git checkout v1.23.0 && docker compose up -d --build`. Restaurez au besoin une
sauvegarde antérieure (Réglages › Sécurité et sauvegardes) : une base ouverte par une version récente reste
lisible par une plus ancienne, les colonnes ajoutées étant simplement ignorées.

## 1.25.1 — 14 novembre 2026
- **Mises à jour de sécurité automatiques** : l'image est reconstruite chaque semaine avec les correctifs de
  Debian, Python et pip, puis analysée (Trivy) ; une faille grave corrigeable vous est signalée par e-mail.
  Sur le serveur, `sudo deploy/install-auto-update.sh` installe une mise à jour nocturne qui remet l'image
  précédente en service si la nouvelle ne démarre pas.
- `docker-compose.yml` utilise désormais l'image publiée (mise à jour : `docker compose pull && docker compose up -d`).
- Tests aussi sous Python 3.15, la version de l'image ; actions de la CI épinglées par empreinte.

## 1.25.0 — 14 novembre 2026
- **Brouillons de réponse par l'IA** (À traiter) : pour une réponse qu'aucune réponse type ne couvre, un modèle
  conversationnel rédige un brouillon. Vous le relisez, le corrigez et l'envoyez ; rien ne part sans vous.
- **Comparateur de modèles** (Réglages › Automatismes) : fait classer vos propres réponses par plusieurs modèles
  et mesure l'accord avec les règles, le temps par réponse et la mémoire. LFM3 ajouté aux modèles proposés.
- **Démarchage téléphonique** (Dossiers RGPD) : dossier prêt après un appel, avec le courrier (opposition,
  effacement, origine du numéro, preuve du consentement) et le texte du signalement SignalConso.

## 1.24.0 — 13 novembre 2026

Sécurité et vie privée
- **L'IA ne clôt plus une demande à elle seule.** Quand elle conclut « aucune donnée » ou « données supprimées »
  sans qu'une règle l'ait reconnu, la demande passe en « Réponse à examiner » avec son analyse.
- **Content-Security-Policy** sur toutes les pages : seuls les scripts de l'appli s'exécutent.
- **Conservation limitée (facultative)** : Réglages › Sécurité et sauvegardes, durée en mois. Désactivée par
  défaut. Efface le contenu des e-mails des demandes terminées ou annulées, jamais celles refusées ou portées
  devant la CNIL.
- **Conteneur durci** : système de fichiers en lecture seule, aucun privilège, mémoire plafonnée à 1 Go.
- Dépendances figées, audit de vulnérabilités en CI, mises à jour proposées chaque semaine (Dependabot).

Fiabilité et vitesse
- **Historique structuré** : chaque événement note qui a décidé (règles, IA, réanalyse, vous) et quoi. Le code
  ne dépend plus du libellé des phrases de l'historique. Les anciens événements sont complétés au démarrage.
- **À traiter plus rapide** : le classement d'une réponse est calculé une fois (2,1 s → 0,1 s avec 200 demandes).

Code
- `main.py` découpé en modules de pages (`app/routes/`), règles de classement dans `rules.py`.
- `tools/replay_export.py` : rejoue les règles sur un export « À traiter ».
- Lint élargi (ruff), tests du catalogue de courtiers et du planificateur.

## 1.23.0 — 31 octobre 2026
- Réglages › Sobriété énergétique : fréquence de relève, IA sur processeur ou carte graphique (Vulkan), profil
  d'énergie du serveur, avec les commandes de correction.

## 1.22.0 — 31 octobre 2026
- « Actions requises » découpée par type : formulaire, lien, réponse prête, pièce d'identité, autre adresse,
  identifiant publicitaire.

## 1.21.0 — 31 octobre 2026
- Réponses types aux questions simples des courtiers (lien avec l'entreprise, compte, adresse concernée),
  envoyées en un clic ou automatiquement.

## 1.20.0 — 31 octobre 2026
- Lien de confirmation retrouvé dans un message précédent du même courtier.

## 1.19.0 — 31 octobre 2026
- Confirmation envoyée depuis une autre adresse du profil quand un courtier l'exige.
- Courriers : nom donné aussi sans accents.

## 1.18.0 — 31 octobre 2026
- Page « Identifiants publicitaires » : marche à suivre commune et clôture groupée.

## 1.17.0 et 1.17.1 — 31 octobre 2026
- Classement revu sur 78 réponses réelles : notre lettre citée n'est plus lue comme la réponse, nouvelles
  catégories « formulaire exigé » et « identifiant publicitaire », non-distributions mieux triées.
- Réanalyse : ne rouvre plus les bonnes clôtures, reconnaît les non-distributions depuis l'historique.

## 1.16.0 — 31 octobre 2026
- Export pour analyse (à traiter, terminées, traitées sans vous), avec masquage des données personnelles.

## 1.15.0 — 30 octobre 2026
- Recherche et filtres instantanés.

## 1.0 à 1.14 — 29 et 30 octobre 2026
- Première version : demandes d'effacement aux courtiers, relève et classement des réponses, relances.
- Recherche globale, dossiers RGPD, cartographie des expositions, ménage des boîtes mail.
- Hotmail par connexion Microsoft, migration des comptes vers des alias Proton, IA locale (Ollama),
  réanalyse, « En bref », thème clair/sombre, traduction anglaise.
