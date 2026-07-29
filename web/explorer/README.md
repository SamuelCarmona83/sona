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

```bash
python3 web/server.py
# → http://localhost:8080/
```

## Stack

- Vue 3 (Composition API, `<script setup>`)
- Vite 8
- Tailwind CSS 4 (`@tailwindcss/vite`)
- TypeScript
