# Security policy

## Responsible disclosure

Please **do not publish exploit details or credentials in a public issue**. Use GitHub's private vulnerability reporting when enabled by the repository owner. Otherwise, open a public issue requesting a private contact method **without providing exploit details**.

## Deployment guidance

- Never commit `.env`, production databases, logs, API tokens or screenshots of provider credentials.
- If a Free Mobile API key is leaked, rotate it in the Free subscriber dashboard before further use.
- Generate a unique high-entropy bearer token for each client. Remove old tokens when no longer needed.
- Keep the service on localhost, Tailscale, WireGuard or behind a TLS-terminating reverse proxy with additional controls.
- Protect Docker socket, volume contents and host backups. Host administrators can read secrets.
- Do not include sensitive information in SMS content; SMS itself is not end-to-end encrypted.
- Ensure that the single worker is not duplicated across deployments sharing the same database.
- Monitor disk usage and review retention: automatic cleanup and metrics are not implemented yet.
- Do not use this service as your only emergency notification path.