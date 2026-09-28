# Deploying on Render

`render.yaml` at the repo root is a Render Blueprint for the same stack `docker-compose.yml` runs
locally: the player (`ultrademo-web`), the api and operator as private services, the session agent
as a background worker, Postgres 18, and the sample product as a static site.

## Before you start

- **LiveKit.** Render has no inbound UDP, so it cannot host the LiveKit media server. Create a
  LiveKit Cloud project (or run LiveKit elsewhere) and have its `wss://` URL, API key and API
  secret ready.
- **Keys:** Anthropic and ElevenLabs. ElevenLabs does both speech to text and text to speech; to
  use Deepgram for speech to text instead, set `ULTRADEMO_AGENT_STT_PROVIDER=deepgram` and add
  `DEEPGRAM_API_KEY` on the agent.

## Create it

Render dashboard > New > Blueprint > this repo. Render generates `ULTRADEMO_INTERNAL_TOKEN` and
`ULTRADEMO_RECEIPT_SECRET` and asks for the rest:

| Variable | Services |
| --- | --- |
| `ANTHROPIC_API_KEY`, `ELEVEN_API_KEY` | agent |
| LiveKit URL (`wss://…`) | web, api, operator, agent |
| LiveKit API key and secret | api, operator, agent |
| `ULTRADEMO_PUBLIC_BASE_URL` (the web service's `https://…onrender.com` URL) | api |

The api runs its migrations as Render's pre-deploy command, before each deploy goes live.

## Load the example demo

The example points at the compose hostname, so point it at the deployed sample product, then
bootstrap from the api's Shell tab:

```sh
sed -i 's#http://sample-product:8080#https://ultrademo-sample-product.onrender.com#' examples/acme.json
python -m ultrademo_api.bootstrap examples/acme.json
```

Then open `https://<web service>.onrender.com/d/acme-crm-demo`. Render may add a suffix to a
service's `.onrender.com` name when it is taken; use the URLs the dashboard shows.

## Sizing

The operator runs one Chromium per live demo (about 500 MB each), so it is on the `standard` plan
with `ULTRADEMO_OPERATOR_MAX_SANDBOXES=2`. Raise both together. Render cannot set `shm_size`;
Chromium already runs with `--disable-dev-shm-usage`.
