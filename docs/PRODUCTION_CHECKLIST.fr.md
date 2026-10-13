# ✅ Checklist de mise en production — Free SMS Gateway

> **Périmètre** : passerelle auto-hébergée FastAPI + SQLite + Docker, utilisant l'API *Notifications par SMS* de Free Mobile pour envoyer des alertes **uniquement vers le numéro de l'abonné**. Le projet n'est pas affilié à Free Mobile.
>
> **Statut initial (13 octobre 2026)** : prototype public, **pas encore validé en production**. Une case cochée doit correspondre à une preuve vérifiable, pas à une hypothèse. Les 10 tests unitaires précédemment exécutés sont simulés et ne prouvent pas la réception réelle d'un SMS.

## Règle GO / NO-GO

- **NO-GO pour l'alerte critique** tant qu'une seule tâche **P0** ci-dessous manque : elle peut entraîner une alerte perdue, un doublon, une divulgation de secret, ou une absence de détection de panne.
- **GO limité pour un pilote personnel** : seulement après sécurisation réseau, sauvegarde, test SMS réel, et avec un autre moyen de vérifier les incidents importants. Ne pas annoncer de garantie de livraison.
- **GO production** : toutes les tâches P0 validées, scénarios de panne testés, supervision externe active, responsable et procédure de retour arrière identifiés.

## 1. Correctifs applicatifs bloquants — P0

- [ ] **Limiter toutes les tentatives d'envoi, pas seulement les succès.** Le worker utilise actuellement MAX(sent_at) sur status=sent : vérifier et corriger les rafales après HTTP 402, 5xx ou échec d'envoi. Tester le rythme avec une API Free simulée.
- [ ] **Décider explicitement du traitement des réponses HTTP ambiguës (5xx et autres).** Le code réessaie certains statuts : exiger une politique documentée de doublons possibles, ou basculer vers un état à vérifier. Ne pas présumer qu'un 5xx garantit l'absence de SMS.
- [ ] **Détecter les événements bloqués** en queued, sending, failed ou uncertain, et déclencher une alerte **hors de cette même passerelle** (surveillance externe et canal de secours).
- [ ] **Ajouter une durée de validité par alerte** ou une politique équivalente : ne jamais envoyer des heures après leur création des alertes d'incident devenues obsolètes. Tester après coupure prolongée.
- [ ] **Ajouter une limite de croissance SQLite/file d'attente** : nettoyage des événements terminés avec rétention définie, seuil disque, comportement explicite si stockage saturé.
- [ ] **Séparer liveness et readiness** : /healthz ne démontre aujourd'hui que la disponibilité HTTP, pas la santé du worker, de SQLite ni de Free. Détecter worker arrêté, base non accessible et backlog anormal.
- [ ] **Tester la reprise après crash** : arrêt pendant l'envoi, redémarrage, SQLite WAL et état sending -> uncertain ; vérifier l'absence de réémission aveugle.
- [ ] **Revoir la stratégie de quotas** : distinguer quota interne (valeurs configurées) des limites contractuelles/techniques imposées par Free ; documenter les comportements quand le quota est atteint ou quand les urgences dépassent les messages ordinaires.

## 2. Secrets et surface d'exposition — P0

- [ ] **Régénérer la clé API Free** si elle a été affichée dans une capture, un chat, un terminal partagé ou un journal. Ne pas considérer l'ancien secret comme sûr.
- [ ] **Configurer FREE_USER, FREE_API_KEY et CLIENT_KEYS** dans un .env local hors Git ; chaque client reçoit un jeton aléatoire distinct d'au moins 32 caractères. Remplacer tous les placeholders.
- [ ] **Contrôler la confidentialité des secrets** : droits sur .env, sauvegardes et volumes ; aucun secret dans commits, images Docker, CI, logs, issue ou screenshot.
- [ ] **Conserver l'API privée** : 127.0.0.1, tunnel SSH ou réseau privé. Si exposition distante : proxy HTTPS, authentification forte, filtrage réseau et vérification TLS de bout en bout. **Jamais** de HTTP public sur 8080.
- [ ] **Sécuriser le VPS** : mises à jour, SSH par clés, accès administrateur limité, pare-feu, journalisation et comptes séparés.
- [ ] **Démontrer l'isolation par client** : un token A ne peut pas consulter les événements du client B ; tokens invalides -> 401, IDs inconnus -> 404.
- [ ] **Prévoir révocation et rotation** d'une clé client/API Free sans garder d'ancienne clé dans l'historique ni provoquer une longue interruption.

## 3. Données, sauvegarde et retour arrière — P0

- [ ] **Valider la persistance** : la base utilise un volume Docker dédié ; après un redémarrage, les alertes en file et leurs états restent cohérents.
- [ ] **Configurer des sauvegardes cohérentes SQLite** (API SQLite backup ou autre procédure compatible WAL, pas une simple copie non coordonnée de la base active).
- [ ] **Tester au moins une restauration sur environnement isolé** et mesurer RPO / RTO réels.
- [ ] **Définir la politique de rétention** des textes de SMS, journaux et sauvegardes ; éviter d'envoyer des données personnelles ou secrets dans message.
- [ ] **Préparer un rollback** : image ou commit précédent figé, sauvegarde avant migration, procédure de restauration ; tenir compte d'éventuelles évolutions du schéma SQLite.

## 4. Validations techniques et SMS réels — P0

- [ ] **Verrouiller une révision** (commit SHA / release) et contrôler l'intégrité des sources déployées.
- [ ] **Vérifier GitHub Actions** sur cette révision : workflow exécuté et vert. Le fichier de workflow seul ne suffit pas.
- [ ] **Exécuter la suite de tests en local/CI** et conserver son résultat ; ajouter des tests concurrence, déduplication, limites, quotas, erreurs HTTP, disque plein et redémarrage.
- [ ] **Valider la configuration Docker** avec les commandes du bloc « Smoke test ».
- [ ] **Envoyer un SMS de test de bout en bout** sur la ligne associée à la clé Free. Vérifier 202 -> sent et *réception effective sur le téléphone* ; le statut sent ne prouve que l'acceptation HTTP par Free.
- [ ] **Vérifier les erreurs prévisibles** : token erroné (401), payload invalide (422), quota client (429), identifiants Free invalides (403), rafale Free (402), panne réseau/timeouts, et refus HTTP inattendu, **sur serveur de test/mock** pour éviter des SMS non désirés.
- [ ] **Mesurer les temps** entre création, tentative, réponse Free et réception du SMS sur plusieurs essais autorisés, sans supposer d'engagement SLA de Free.
- [ ] **Effectuer un canary** : commencer avec un seul client non critique pendant au moins 24 h et surveiller logs, base, CPU, mémoire, disque, quotas, duplications et tentatives bloquées.

## 5. Supervision & opérations — P0

- [ ] **Mettre en place une supervision externe au VPS** surveillé et à la passerelle (sonde d'accès, métriques de backlog, alertes failed/uncertain et délai depuis la dernière tentative).
- [ ] **Prévoir un canal de secours indépendant** (mail, Telegram ou autre service) pour les incidents prioritaires et la panne du VPS. Le secours ne doit pas dépendre du même processus.
- [ ] **Tester une panne réelle simulée** : arrêter un service surveillé, constater la détection, le déclenchement, l'envoi et la récupération ; répéter quand la passerelle elle-même est indisponible.
- [ ] **Définir un opérateur et un runbook** : que faire pour 402, 403, 5xx, timeout, disque plein, débordement de queue, clé compromise, restauration.
- [ ] **Définir des seuils mesurables** : taille maximale de file, ancienneté maximale d'une alerte urgente, taux d'échec, délai d'escalade. Ce sont des objectifs internes, non des garanties Free.
- [ ] **Contrôler les logs** : conserver horodatage, ID d'alerte, client et état sans y publier identifiants Free, clé API ou contenu confidentiel.

## 6. Qualité de publication et maintenance — P1

- [ ] Vérifier les *Topics*, la description About, le README français/anglais et les liens sur GitHub.
- [ ] Publier une première release versionnée seulement après validation du périmètre correspondant (ex. v0.1.0 = prototype, sans revendication « production ready »).
- [ ] Activer alertes de dépendances / Dependabot et revue régulière des versions Python/FastAPI/httpx.
- [ ] Mettre en place analyse statique, scan secrets et scan des dépendances dans CI.
- [ ] Documenter les contraintes d'usage de l'API Free (usage personnel, destinataire unique, éventuelles limites évolutives).
- [ ] Ajouter des exemples d'intégration génériques et des guides d'exploitation ; ne pas inclure de noms de clients ou de services privés.

## Smoke test (VPS ou environnement de test)

**Attention :** ne collez pas vos vraies clés API dans une issue GitHub ni une sortie partagée.

```bash
git clone https://github.com/DissidentAI/free-sms-gateway.git
cd free-sms-gateway
cp .env.example .env
chmod 600 .env
# Renseigner .env en privé avec ses vraies valeurs (jamais les commit).
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
docker compose logs --tail=40 gateway
```

Vérifiez que la réponse de healthz est `{"ok":true}` **sans la confondre avec une garantie d'envoi**. Pour le test fonctionnel, utilisez l'exemple POST du README avec un jeton client valide ; conservez l'ID renvoyé, interrogez `GET /v1/alerts/{id}`, puis **confirmez physiquement le SMS sur le téléphone**.

**Ne lancez pas le déploiement avant d'avoir remplacé les placeholders de .env.**

## Fiche de validation (à remplir sans secret)

| Élément | Valeur |
| --- | --- |
| Environnement / hôte (identifiant non sensible) | À renseigner |
| Commit déployé | À renseigner |
| Date et heure de validation | À renseigner |
| Responsable validation | À renseigner |
| Version Docker / Python | À renseigner |
| CI GitHub Actions | À confirmer, lien vers run |
| Test SMS Free (heure / statut / réception) | À confirmer |
| Tests pannes et restauration | À confirmer |
| Monitor externe / canal secondaire | À confirmer |
| Backups / RPO / RTO | À confirmer |
| Résultat final | **NO-GO** par défaut tant que P0 incomplets |
| Dérogations acceptées et risques | À documenter |

---

**Note importante :** cette checklist est un guide de vérification, pas un certificat de sécurité ni une preuve de livraison. Des cases restent volontairement décochées même si le code offre déjà une partie du comportement : seule une vérification sur l'environnement cible les validera.
