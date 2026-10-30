(function () {
  "use strict";
  const KEY = "oubliez-moi-language";
  const translations = {
    "Tableau de bord":"Dashboard","Profils":"Profiles","Demandes":"Requests","Dossiers RGPD":"GDPR cases",
    "À traiter":"Needs attention","Courtiers":"Data brokers","Boîtes mail":"Mailboxes","Expositions":"Exposure map",
    "Réglages":"Settings","Déconnexion":"Log out","Rechercher…":"Search…","Recherche globale":"Global search","courtiers ciblés":"brokers targeted","e-mails/heure max.":"emails/hour max.",
    "Envoyer le suivant":"Send next","Relever la boîte":"Check mailbox","Mode simulation.":"Simulation mode.",
    "Aucun e-mail n'est réellement envoyé.":"No email is actually sent.",
    "Une fois la messagerie testée et le courrier relu, passez en envoi réel dans":"After testing your mail settings and reviewing the message, switch to live sending in",
    "Messagerie non configurée.":"Email is not configured.","Indiquez l'adresse e-mail qui enverra les demandes dans":"Set the email address that will send requests in",
    "Bienvenue 👋":"Welcome 👋","Commencez par créer un profil : votre nom, vos adresses e-mail, téléphones et adresses postales.":"Start by creating a profile with your name, email addresses, phone numbers and postal addresses.",
    "Ces informations servent uniquement à vous identifier auprès des courtiers.":"This information is only used to identify you to data brokers.",
    "Créer mon profil":"Create my profile","Profils suivis":"Profiles tracked","Boîtes mail connectées":"Connected mailboxes",
    "Expéditeurs détectés":"Senders detected","Expéditeurs à traiter":"Senders needing review","Suppressions confirmées":"Deletions confirmed",
    "En attente de réponse":"Awaiting reply","Refus / sans réponse":"Refused / no reply","terminées":"completed",
    "en attente":"awaiting reply","à traiter":"needs attention","en échec":"failed","en file":"queued",
    "Prochaines échéances":"Upcoming deadlines","Aucune demande en attente.":"No pending requests.",
    "Activité récente":"Recent activity","Rien pour l'instant.":"Nothing yet.",
    "Dernière relève IMAP :":"Last IMAP check:","IMAP non configuré : les réponses doivent être reportées à la main.":"IMAP is not configured: replies must be recorded manually.",
    "Une personne par profil (vous, votre conjoint·e, vos parents…).":"One person per profile (you, your partner, your parents…).",
    "+ Nouveau profil":"+ New profile","Nom":"Name","E-mails":"Emails","Avancement":"Progress",
    "terminé":"complete","pas lancé":"not started","Modifier":"Edit","Aucun profil.":"No profiles yet.","Créer un profil":"Create profile",
    "résultat(s)":"result(s)","Réessayer les échecs":"Retry failed","Exporter (CSV)":"Export (CSV)",
    "Courtier, e-mail ou référence":"Broker, email or reference","Tous les statuts":"All statuses",
    "Tous les profils":"All profiles","Filtrer":"Filter","Réf.":"Ref.","Courtier":"Data broker",
    "Profil":"Profile","Statut":"Status","Envoyée":"Sent","Échéance":"Deadline","Réponse":"Reply","relancée":"reminded",
    "Aucune demande.":"No requests.","Suivez les demandes adressées aux organismes, leurs réponses et leurs délais.":"Track requests sent to organizations, their replies and deadlines.",
    "Nouvelle demande":"New request","Organisme *":"Organization *","Type de demande *":"Request type *",
    "Objet ou périmètre":"Subject or scope","Profil concerné":"Related profile","facultatif":"optional",
    "Aucun profil associé":"No profile linked","Référence communiquée par l’organisme":"Reference provided by the organization",
    "Note initiale":"Initial note","Créer le dossier":"Create case","Ajoutez d’abord un organisme pour créer un dossier.":"Add an organization first to create a case.",
    "Ajouter un organisme":"Add an organization","Nom de l’organisme *":"Organization name *","Site web":"Website",
    "Adresse de contact RGPD":"Privacy contact email","Adresse postale":"Postal address","Notes privées":"Private notes",
    "Enregistrer l’organisme":"Save organization","Suivi des dossiers":"Case tracking",
    "Organisme / objet":"Organization / subject","Demande":"Request","Aucun dossier ne correspond à ces critères.":"No cases match these filters.",
    "Créer un dossier n’envoie aucun courrier. L’envoi et la confirmation du résultat restent des actions explicites de votre part.":"Creating a case does not send anything. Sending and confirming the outcome remain actions you must explicitly take.",
    "Cartographie des expositions":"Exposure map","Reliez les domaines qui vous écrivent aux organismes connus du catalogue. Les correspondances automatiques sont des pistes à vérifier, pas des preuves de partage de données.":"Link the domains that email you to organizations in the catalog. Automatic matches are leads to review, not proof of data sharing.",
    "Domaines observés":"Domains observed","Sans lien identifié":"No identified link","Correspondances à vérifier":"Matches to review",
    "Expéditeurs revenus après désinscription":"Senders returning after unsubscribe","Associer un domaine manuellement":"Manually link a domain",
    "Domaine observé":"Observed domain","Organisme du catalogue":"Catalog organization","Groupe / marque commune":"Group / shared brand",
    "Facultatif":"Optional","Justification / source":"Reason / source","Confirmer l’association":"Confirm link",
    "Tous les liens":"All links","À vérifier":"Needs review","Confirmer":"Confirm","Écarter":"Reject",
    "Modifier le groupe":"Edit group","Aucune correspondance connue.":"No known matches.",
    "Aucun domaine observé pour le moment. Ajoutez une boîte mail et lancez une analyse pour commencer la cartographie.":"No domains observed yet. Add a mailbox and run an analysis to start mapping.","Enregistrer":"Save","Général":"General","Données":"Data","Sauvegardes":"Backups",
    "Identifiants incorrects.":"Incorrect credentials.","Connexion":"Log in","Mot de passe":"Password",
    "Se connecter":"Sign in","Au moins une adresse e-mail est nécessaire pour que les courtiers vous identifient.":"At least one email address is required so data brokers can identify you.",
    "Aucune sauvegarde pour l'instant.":"No backups yet.","Exporter l'historique (CSV)":"Export history (CSV)","Annuler":"Cancel","Supprimer":"Delete","Ajouter":"Add",
    "Précédent":"Previous","Suivant":"Next",
    "Rechercher":"Search","Date":"Date","Actions":"Actions","Détails":"Details","Historique":"History","Créer":"Create","Non":"No",
    "Effacement chez les courtiers":"Data broker removals","Ménage des boîtes mail":"Mailbox clean-up",
    "Autres droits RGPD":"Other GDPR rights","Aller au contenu":"Skip to content","Fermer le menu":"Close menu",
    "Navigation principale":"Main navigation","Langue":"Language","Boîte des demandes":"Requests mailbox",
    "Pas encore analysée : rien n'est lu tant que vous ne lancez pas l'analyse.":"Not analysed yet: nothing is read until you start the analysis.",
    "L'ajouter à la liste":"Add it to the list","Aussi la boîte qui relève les réponses des courtiers":"Also the mailbox that collects data broker replies",
    "Migration des comptes":"Account migration","Inventorier mes comptes":"Scan my accounts","Les boîtes à inventorier":"Mailboxes to scan",
    "Adresse à quitter":"Address to leave","Nouvelle adresse (Proton)":"New address (Proton)","Ne pas utiliser":"Don't use",
    "Vos alias Proton Pass":"Your Proton Pass aliases","Vérifier les alias":"Check aliases","À migrer":"To migrate",
    "Alias prêt, adresse à changer":"Alias ready, address to change","Alias prêt":"Alias ready","Migrés":"Migrated","Migré":"Migrated",
    "À abandonner":"To abandon","Pas des comptes":"Not accounts","Pas un compte":"Not an account","Comptes seulement":"Accounts only",
    "Créer l'alias":"Create alias","Saisir un alias":"Enter an alias","Copier":"Copy","Marquer comme migré":"Mark as migrated",
    "Ce n'est pas un compte":"Not an account","Statut automatique":"Automatic status","Retirer l'alias":"Remove alias",
    "Réglages de la migration":"Migration settings","Rôle des boîtes":"Mailbox roles","Recommencer l'inventaire":"Restart the scan",
    "Ancienne adresse":"Old address","Service":"Service",
    "Thème auto":"Auto theme","Clair":"Light","Sombre":"Dark","Voir la mise en forme d'origine":"Show original formatting",
    "Réanalyse en cours":"Re-analysis in progress","Réanalyser les réponses":"Re-analyse replies",
    "Classées par l'IA":"Classified by AI","Classé par les règles":"Classified by rules","Début du message :":"Message starts with:",
    "Conseil de l'IA :":"AI advice:","À faire :":"To do:"
  };
  // Les mots isolés (« Nom », « Non », « Profil »…) ne sont traduits que si le texte entier
  // leur est égal : sinon « Nombre » deviendrait « Namebre ». Les expressions sont remplacées
  // seulement entre deux limites de mot.
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const exact = new Map();
  const phrases = [];
  for (const [fr, en] of Object.entries(translations)) {
    if (/\s/.test(fr.trim())) phrases.push([new RegExp("(?<![\\p{L}\\p{N}])" + escapeRe(fr) + "(?![\\p{L}\\p{N}])", "gu"), en, fr.length]);
    else exact.set(fr, en);
  }
  phrases.sort((a, b) => b[2] - a[2]);
  function translateText(value) {
    const trimmed = value.trim();
    if (!trimmed) return value;
    if (exact.has(trimmed)) return value.replace(trimmed, exact.get(trimmed));
    let result = value;
    for (const [re, en] of phrases) result = result.replace(re, en);
    return result;
  }
  // Jamais de traduction dans le contenu de l'utilisateur ou des courtiers : e-mails, courriers
  // (y compris le texte à coller dans un formulaire), notes, champs de saisie.
  const SKIP = "script,style,textarea,pre,code,.mail-body,.mono,[translate='no']";
  function apply(lang) {
    document.documentElement.lang = lang;
    const selector = document.getElementById("language-switch");
    if (selector) selector.value = lang;
    if (lang !== "en") return;  // la page est servie en français : rien à faire
    document.title = translateText(document.title);
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (node.parentElement && !node.parentElement.closest(SKIP)) node.nodeValue = translateText(node.nodeValue);
    }
    for (const el of document.querySelectorAll("[placeholder],[aria-label],[title]")) {
      const protectedBlock = el.closest(SKIP);
      if (protectedBlock && protectedBlock !== el) continue;  // un champ de saisie garde son libellé traduit
      for (const attr of ["placeholder", "aria-label", "title"]) {
        if (el.hasAttribute(attr)) el.setAttribute(attr, translateText(el.getAttribute(attr)));
      }
    }
  }
  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }  // stockage indisponible (navigation privée…)
  }
  let lang = stored();
  if (!["fr","en"].includes(lang)) lang = (navigator.language || "").toLowerCase().startsWith("en") ? "en" : "fr";
  document.addEventListener("DOMContentLoaded", () => {
    apply(lang);
    const selector = document.getElementById("language-switch");
    if (selector) selector.addEventListener("change", () => {
      lang = selector.value;
      try { localStorage.setItem(KEY, lang); } catch (e) { /* choix non mémorisé */ }
      location.reload();
    });
  });
})();