# Relicta
> *relictus* (Latin) — abandoned, left behind

Relicta is a self-hosted marketplace monitor for trusted local networks that helps you find matching listings across Bazoš, Sbazar, eBay, Facebook Marketplace, and custom sources. (This project is almost entirely vibecoded so use at your own risk)

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

## License

Relicta is available under the [MIT License](LICENSE.txt).
