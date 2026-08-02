# Sona explorer (Vue 3)

Data explorer SPA — Vue 3 + Vite + Tailwind v4. Talks to the Python API in `web/server.py`.

## Dev

```bash
# Terminal A — API + cache static files
python3 web/server.py

# Terminal B — Vite HMR (proxies /api and cache dirs)
cd web/explorer
npm install
npm run dev
```

Open the URL Vite prints (default http://localhost:5173).

## Production build

```bash
cd web/explorer
npm install
npm run build   # → dist/
```

`web/server.py` serves `dist/` at `/` when present; otherwise falls back to legacy `web/explorer.html`.
Paths like `/web/explorer.html` redirect to `/` when Vue is built.

```bash
python3 web/server.py
# → http://localhost:8080/
# → http://localhost:8080/api/health  → {"ui":"vue"| "legacy", ...}
```

### Docker

`dist/` is **gitignored**. The image builds the SPA in a multi-stage `Dockerfile`.

**Do not** mount `./web` over `/app/web` in compose — that hides the image `dist/` and forces legacy HTML.

```bash
docker compose build explorer
docker compose up -d explorer
# open http://<host>:8080/   (not /web/explorer.html)
```

## Stack

- Vue 3 (Composition API, `<script setup>`)
- Vite 8
- Tailwind CSS 4 (`@tailwindcss/vite`)
- TypeScript

## UX notes

- Design system: [`../DESIGN.md`](../DESIGN.md) (manpage / mono / cream)
- Deep links: `?tab=library&view=table&q=radio`
- Shortcuts: `/` focuses filter; `1`–`4` switch tabs (when not typing)
- Destructive actions use an in-app confirm modal (delete, dedupe, enrich)
