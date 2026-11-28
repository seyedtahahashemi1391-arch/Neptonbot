# Portable deployment

NeptonBot can run on a Linux host or VPS with Docker Compose and PostgreSQL. These files prepare the application for relocation; they do not prove that the target host is reachable through Iran's National Information Network.

## 1. Prepare the host

1. Install Docker Engine and the Docker Compose plugin.
2. Copy `.env.example` to `.env`.
3. Set `BOT_TOKEN`.
4. Set `POSTGRES_PASSWORD` to a long URL-safe random value. A 64-character hexadecimal value avoids URL-encoding problems in the generated PostgreSQL connection string.
5. Keep `.env` private; never commit real tokens or passwords.

## 2. Choose the AI provider

### Existing Cloudflare provider

Keep the defaults and set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`.

### Private or domestic OpenAI-compatible provider

The model endpoint must implement `POST /v1/chat/completions` and return the standard OpenAI chat-completions response shape.

Set these values in `.env`:

```dotenv
NEPTON_AI_PROVIDER=openai-compatible
NEPTON_AI_BASE_URL=http://host.docker.internal:11434/v1/chat/completions
NEPTON_AI_API_KEY=
NEPTON_MODEL=qwen2.5:7b
NEPTON_AI_MODE=auto
```

The example URL assumes an AI server listening on port 11434 on the Docker host. Replace the URL and model with the actual endpoint and model supplied by the provider. API keys are optional for private endpoints that do not require authentication. Do not expose an unauthenticated model endpoint to the public internet.

For an AI server on another private host, set `NEPTON_AI_BASE_URL` to its private address, for example `http://10.0.0.20:8000/v1/chat/completions`.

## 3. External live-data dependencies

Set `NEPTON_LIVE_DATA_MODE=off` when external connectivity is unavailable. This prevents calls to:
- Brave Search
- Open-Meteo and the wttr.in weather fallback
- The GitHub-hosted Iran market data feed

In this mode, the bot clearly reports that live data is unavailable instead of inventing prices, weather, or search results. The deterministic `NEPTON_AI_MODE=rules` mode can also disable model requests, but provides only basic fixed replies and is not a substitute for a local language model.

## 4. Start the stack

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

The HTTP liveness endpoint is available at `http://HOST:10000/health` by default. Change `PORT` in `.env` to publish a different host port.

## 5. Update and back up

```sh
docker compose up -d --build
```

Back up the `nepton_postgres_data` volume before upgrades or migrations. Do not remove the volume when recreating containers.

## National Information Network readiness checklist

The following checks are mandatory before describing the deployment as NIN-compatible:

1. Run the stack on a host physically/network-located inside Iran.
2. From that host, verify DNS resolution and HTTPS connectivity to the official Soroush Plus Bot API at `api.splus.ir`. The bot cannot receive or send messages if this endpoint is unreachable.
3. Verify that the configured AI endpoint is reachable from the bot container without international routing. A domestic/private endpoint is preferred.
4. Set `NEPTON_LIVE_DATA_MODE=off`, or replace every live-data provider with an endpoint verified to work on the national network.
5. Test polling, message sending, database persistence, restart recovery, and queue processing while international connectivity is unavailable.
6. Confirm that the host, DNS resolver, TLS certificate chain, database, and model endpoint remain available during the intended network conditions.

A configurable Soroush API base URL is provided through `NEPTON_SOROUSH_API_BASE`; keep the official default unless Soroush Plus supplies an authorized alternative. Do not point it to an unofficial mirror.

## Known limitations

- Docker packaging is portable, but it does not supply an Iranian host, a domestic AI model, or a national-network route.
- Soroush Plus API access is a hard dependency for the current Soroush adapter.
- Local AI quality and performance depend on the selected model and host hardware.
- Live web search, weather, and market data are intentionally unavailable when `NEPTON_LIVE_DATA_MODE=off`.
- This branch does not change the production Render service or the `main` branch.
