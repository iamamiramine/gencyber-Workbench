# Gencyber Workbench

Single Docker image that runs:

- **FastAPI** on port **80** — challenge catalog, metadata, optional seed prompt, and **`POST /benchmarks/materialize`** to write decoded challenge files under `CHALLENGE_ROOT` (default `/workspace/challenges`).
- **terminal-session** (Node) on port **3000** — persistent PTY shells with **`POST /api/execute`** for agents (`TERMINAL_SESSION_URL`).

Both processes share the **`/workspace`** volume. The terminal defaults its shell cwd to **`/workspace`** so `ls` shows `challenges/` after materialization.

## Quick start

Create the shared Docker network once if needed: `docker network create generative-cybersecurity-network`.

**NYUCTF data on the host:** clone or copy the dataset repo into a directory on your machine, then point compose at it (defaults to `./nyuctf-data` next to this file):

```bash
export NYUCTF_HOST_PATH=/absolute/path/to/your/nyuctf-v20250206   # optional; default ./nyuctf-data
docker compose up --build
```

That path is mounted at **`/root/.nyuctf/v20250206`** inside the container so the `nyuctf` package and materialize see the same tree as on the host.

- Challenge API: `http://localhost:8080` (maps container port 80).
- Terminal UI / API: `http://localhost:3000`.

Materialize a NYUCTF challenge (example):

```bash
curl -sS -X POST "http://localhost:8080/benchmarks/materialize" \
  -H "Content-Type: application/json" \
  -d '{"benchmark":"nyuctf","split":"development","challenge_id":"<id>"}' | jq .
```

Copy `seed_prompt` from the response into the **Chat** bar in Streamlit, or use **`gencyber-Frontend`** benchmark tab **Materialize** only.

## Agent wiring

Point **`gencyber-Agent`** at the terminal service (same Docker network), for example:

`TERMINAL_SESSION_URL=http://gencyber-workbench:3000`

Shell commands use **`POST /api/sessions`** then **`POST /api/execute`**; there is no `/sandbox/*` path.

## Security

The terminal executes arbitrary shell on the shared volume. Run on a trusted network only.
