# Relicta
> *relictus* (Latin) — abandoned, left behind

Relicta is a self-hosted marketplace monitor for trusted local networks that helps you find matching listings across Bazoš, Sbazar, eBay, Facebook Marketplace, and custom sources.

## Features

- Schedule searches across multiple marketplaces and custom sources.
- Filter listings by price, shipping, and required or excluded keywords.
- Browse matching listings on the dashboard and track qualifying price drops.
- Receive notifications through Telegram, ntfy, or email.

## Quick start

Requires Docker, Docker Compose, and OpenSSL.

1. Clone the repository:

   ```sh
   git clone https://github.com/Omyxun/relicta.git
   cd relicta
   ```

2. Copy `.env.example` to `.env`.
3. Generate a unique secret and paste it into `SECRET_KEY` in `.env`:

   ```sh
   openssl rand -hex 32
   ```

4. Start Relicta:

   ```sh
   docker compose up -d --build
   ```

5. Open [http://localhost:8000](http://localhost:8000) and create your account. The first account is the administrator. From another device on your LAN, use `http://<server-LAN-IP>:8000`.

Source credentials and notification settings are optional; configure them as needed in `.env` or the app's settings. Persistent data is stored in `./data`.

Registration is enabled by default. To restrict it after creating your account, set `ALLOW_REGISTRATION=false` in `.env` and run `docker compose up -d` to recreate the container.

## Local network deployment

The Compose configuration publishes port 8000 on the host's interfaces so other LAN devices can connect. Limit access with your firewall to trusted devices, create the administrator account before sharing access, and do not forward this port from your router to the internet. All registered users should be trusted; custom marketplace sources are shared. Public GitHub source code does not require a publicly accessible app.

HTTP works on your LAN. Use HTTPS through a reverse proxy when you need encrypted traffic; HTTP sends login credentials and session cookies without transport encryption. Configure Uvicorn to trust forwarded headers only from your proxy if it runs outside the container.

For a private ntfy server, set `NTFY_DEFAULT_SERVER_URL` to its URL. To permit additional private servers in personal settings, add their origins to `.env`, for example:

```dotenv
NTFY_ALLOWED_PRIVATE_SERVERS='["http://192.168.1.20:8080"]'
```

Recreate the container after changing `.env`. Unapproved private destinations and redirects are blocked for notifications. Custom website previews accept public websites only.

Keep `.env`, `data/`, and backups private: they contain session secrets, notification credentials, marketplace cookies, and saved listings. Back up `.env` and `data/` with the container stopped for a consistent SQLite backup. To update, back up first, pull the new code, then run `docker compose up -d --build`.

eBay requires Browse API credentials; Facebook may require authenticated browser cookies. Public pages may be blocked or change their markup. Custom sources scan the first search-results page. Telegram, ntfy, and SMTP credentials are needed only for their respective notification channels.

## License

Relicta is available under the [MIT License](LICENSE.txt).
