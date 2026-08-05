# Security

Tidarr Token Helper handles access and refresh tokens. Treat both as passwords.

- Keep the web interface on a trusted local network.
- Do not expose it directly to the public internet.
- Change `APP_PASSWORD` before deployment.
- HTTP Basic Authentication does not encrypt traffic. Use an HTTPS reverse proxy for remote access.
- Never commit a real `auth.json`, access token, refresh token, or password.
- Redact token values and user IDs from screenshots.
- The application decodes the JWT payload but does not verify its cryptographic signature.
- Submitted token values are processed locally by the container and are not intentionally written to logs.
