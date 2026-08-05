# Security

Tidarr Token Helper handles access and refresh tokens. Treat both as passwords.

- Submitted token values are processed locally by the container and are not intentionally written to logs.
- Keep the web interface on a trusted local network.
- Do not expose it directly to the public internet.
- HTTP Basic Authentication does not encrypt traffic. Use an HTTPS reverse proxy for remote access.
- Change `APP_PASSWORD` before deployment.
- The application decodes the JWT payload but does not verify its cryptographic signature.
