# 🇫🇷 Free SMS Gateway — Documentation française

Passerelle de notifications SMS **auto-hébergée**, permettant à plusieurs applications et serveurs d'utiliser l'option **Notifications par SMS** d'un abonnement Free Mobile, sans distribuer la clé Free à chaque service.

📋 **[Checklist de mise en production (GO/NO-GO)](PRODUCTION_CHECKLIST.fr.md)**

> Projet communautaire non affilié à Free Mobile. Les SMS sont envoyés **uniquement sur le numéro de l'abonné** associé à la clé API, jamais à des destinataires arbitraires.

## Fonctionnalités

- Clés Bearer distinctes pour chaque application
- File d'attente SQLite persistante avec priorités
- Quotas, déduplication et idempotence
- Worker d'envoi unique et tentatives contrôlées
- Déploiement Docker isolé, lié à `127.0.0.1`

## Fiabilité et exploitation

- Toutes les tentatives sont enregistrées et prises en compte dans les quotas.
- Les réponses incertaines (`5xx`, erreurs réseau) ne sont pas réessayées automatiquement.
- Les alertes expirent après `ttl_seconds` (une heure par défaut) et la file est bornée.
- `/readyz` détecte les incidents internes, sans promettre la réception du SMS.
- Outils privés : `python scripts/ops.py stats`, `backup` et `acknowledge`.

📘 [Guide d’exploitation et sauvegarde](OPERATIONS.fr.md) · [Checklist de mise en production](PRODUCTION_CHECKLIST.fr.md)

## Installation

Activez **Notifications par SMS** dans votre Espace Abonné Free Mobile et récupérez votre identifiant et votre clé API.

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(36))'
```

Modifiez `.env` : remplacez l'identifiant `FREE_USER`, la clé `FREE_API_KEY` et toutes les valeurs `CLIENT_KEYS` par **des secrets générés distincts d'au moins 32 caractères**.

```bash
chmod 600 .env
docker compose up -d --build
curl -fsS http://127.0.0.1:8080/healthz
```

## Envoyer une alerte

```bash
curl -fsS http://127.0.0.1:8080/v1/alerts \
  -H 'Authorization: Bearer VOTRE_JETON_CLIENT' \
  -H 'Content-Type: application/json' \
  -d '{"severity":"critical","message":"Un service est indisponible","fingerprint":"service-down"}'
```

La réponse HTTP `202` signifie **mise en file d'attente**, pas réception sur le téléphone. Consultez le statut à `/v1/alerts/{id}` avec la même clé client.

## Sécurité et limites

Ne publiez pas directement le port `8080` sur Internet : l'API locale n'utilise pas TLS. Passez par un réseau privé ou un proxy HTTPS durci. Ne versionnez jamais le fichier `.env`. Les textes des alertes sont stockés dans SQLite.

Les erreurs réseau pendant l'envoi sont marquées `uncertain` : le SMS a peut-être été accepté, donc un réessai automatique produirait potentiellement un doublon. Une panne du serveur hébergeant la passerelle nécessite un superviseur externe.

Le projet n'est pas destiné à l'envoi massif ou commercial de SMS ; vérifiez les conditions d'utilisation Free. Pour plus de détails, consultez le [README principal](../README.md).
