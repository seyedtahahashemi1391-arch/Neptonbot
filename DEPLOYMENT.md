# Portable deployment

NeptonBot can be run as a Docker container with PostgreSQL using Docker Compose.

## Prepare

1. Install Docker Engine and the Docker Compose plugin on the target Linux host.
2. Copy `.env.example` to `.env`.
3. Set `BOT_TOKEN` and a strong `POSTGRES_PASSWORD`.
4. Configure an AI provider if required. Set `NEPTON_AI_MODE=rules` to run without Cloudflare AI (responses will be limited to deterministic fallback behavior).

## Run

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

The HTTP liveness endpoint is available at `http://HOST:10000/health` by default. Change `PORT` in `.env` to publish a different host port.

## Update

```sh
docker compose pull
docker compose up -d --build
```

Back up the `nepton_postgres_data` volume before upgrades or migrations. Do not delete the volume when recreating containers.

## Network-readiness limitations

This packaging makes the application portable; it does **not** by itself make the bot operational on Iran's National Information Network.

The Soroush Plus Bot API (`api.splus.ir`) must be reachable from the host for polling and message delivery. Cloudflare AI, Brave Search, Open-Meteo, wttr.in, and the GitHub-hosted market data feed are separate external dependencies. They may be unavailable when international connectivity is disrupted.

Before claiming national-network compatibility, verify Soroush API reachability from a host inside Iran, then configure an AI endpoint reachable on that network and replace or disable external live-data providers. No production Render settings are changed by these files.
