# Deploying the Recourse demo

## Vercel (serverless demo)

The repo ships Vercel-ready: `api/index.py` (WSGI entrypoint), `vercel.json`
(all routes → the Flask app, which also serves the dashboard), and
`.vercelignore`.

**How it works — stated honestly:** Vercel functions have an ephemeral
filesystem, so on cold start the entrypoint deterministically rebuilds the
demo world into `/tmp` (seed 42 + the six curated cases, ~1s) and serves the
normal app against it. Approvals and webhooks persist while an instance is
warm; a cold start rebuilds the identical world. This is a self-resetting
demo, not persistence. `/health` reports `clock_mode:
pinned_to_synthetic_world`.

**Deploy (dashboard flow):**
1. Push the repo to GitHub (already done if you followed the stages).
2. vercel.com → Add New → Project → import
   `sadankumar-20/Recourse_Ai_chargeback`.
3. Framework preset: **Other**. Leave build command and output directory
   EMPTY (there is no build; the function bootstraps itself). Deploy.
4. Open the URL — the first request takes ~2s (cold start), then it's warm.

**Deploy (CLI flow):**
```bash
npm i -g vercel
vercel          # from the repo root; accept defaults, framework: Other
vercel --prod
```

**Optional — real model on the deployed demo:** in Vercel → Project →
Settings → Environment Variables set `RECOURSE_AI_PROVIDER=anthropic` and
`ANTHROPIC_API_KEY=…`, then redeploy. Extraction/link/draft calls then hit
the real API (the gate and all safety rails are identical either way).
Never commit the key.

**Limits to know:** hobby-plan function duration caps apply (the bootstrap
fits comfortably); SQLite writes are per-instance; concurrent instances each
hold their own world — fine for a demo walkthrough, wrong for production.

## If you want persistence instead

Render / Railway / Fly.io run the server as a normal process with a disk:
`pip install -r requirements.txt`, start command
`python3 data/generate.py --seed 42 && python3 scripts/demo_seed.py && python3 scripts/serve.py --port $PORT`
(bind host 0.0.0.0 via a tiny tweak or gunicorn: `gunicorn -w 2 -b 0.0.0.0:$PORT 'app.api:create_app("demo.db", data_dir="data")'`
from `backend/`). Same code, real persistence.
