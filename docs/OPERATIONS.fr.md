# 🛠️ Guide d'exploitation — Free SMS Gateway

**Ce projet reste NO-GO pour des alertes critiques tant que la checklist P0 n'est pas validée.** L'API Free n'envoie des SMS qu'au numéro de l'abonné et ne garantit pas la remise au téléphone.

## Contrôles

```bash
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/readyz
docker compose logs --tail=100 gateway
docker compose exec gateway python scripts/ops.py stats
```

`/healthz` indique simplement que le serveur HTTP répond. `/readyz` renvoie 503 si worker défaillant, base indisponible, retard de file, ou échec/issue incertaine d'une alerte critique récente. Cela ne garantit pas la livraison par Free. Faire superviser `/readyz` **depuis une autre machine** et prévoir un canal de secours indépendant.

## Sauvegarde cohérente de SQLite

Utiliser l'API de sauvegarde SQLite (WAL-compatible), pas une simple copie du fichier de base en cours d'écriture :

```bash
docker compose exec gateway python scripts/ops.py backup /data/alerts-backup.sqlite3
docker compose cp gateway:/data/alerts-backup.sqlite3 ./alerts-backup.sqlite3
chmod 600 ./alerts-backup.sqlite3
```

Le script refuse d'écraser un fichier existant : **choisir un nom différent à chaque sauvegarde**. Copier hors du VPS vers un stockage chiffré, puis **tester une restauration en environnement isolé**. Les sauvegardes contiennent le texte des alertes.

## Traitement des erreurs

| Situation | Procédure |
| --- | --- |
| HTTP 200 | Free a accepté l'appel ; vérifier la réception réelle du SMS |
| HTTP 402 / 429 | Rejet explicite de débit : nouvelle tentative limitée, avec délai |
| HTTP 400 / 401 / 403 | Échec définitif : vérifier paramètres / droits / secrets, sans les publier |
| HTTP 5xx ou erreur réseau | État `uncertain` : ne pas renvoyer sans confirmer la situation (risque de doublon) |
| `queued` trop ancien | Vérifier quota, Free, durée de vie et état du worker |
| `expired` | Ne pas envoyer un incident dépassé ; créer un nouvel incident s'il est encore actif |

## Reconnaître une alerte après vérification

```bash
docker compose exec gateway python scripts/ops.py acknowledge 42 --reason 'Incident reviewed manually'
```

L'opération **ne réémet aucun SMS** et ne prétend pas confirmer la livraison. Elle change uniquement l'état de l'alerte terminale, après une revue par l'opérateur. En cas de besoin, produire un nouvel événement avec un nouvel identifiant de requête et utiliser un autre canal.

## Mise à jour et retour arrière

1. Noter le SHA Git déployé et sauvegarder la base avant mise à jour.
2. Contrôler `docker compose config --quiet`, le fichier `.env` privé (0600), et le port uniquement local ou sur réseau privé.
3. Tester le bon démarrage : liveness, readiness, file, quotas et un SMS réel sur la ligne Free.
4. En cas d'échec, arrêter la passerelle, conserver la base actuelle et **restaurer sur un volume isolé** une sauvegarde compatible avec la version précédente du code.
5. Ne jamais rejouer aveuglément `sending` ou `uncertain` : leur issue est potentiellement ambiguë.

## Préconditions de production

- Secret Free régénéré si déjà affiché dans une capture ; un token distinct par client, jamais commités.
- Isolation réseau, durcissement SSH/pare-feu, métriques et supervision hors du même VPS.
- Restauration testée et canal secondaire (e-mail/Telegram) indépendant.
- Tests d'erreurs, de redémarrage et un test réel de réception confirmés.

Voir la **[checklist GO/NO-GO](PRODUCTION_CHECKLIST.fr.md)**.
