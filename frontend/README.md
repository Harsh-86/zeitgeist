# zeitgeist frontend

The landing page and live graph explorer for [zeitgeistnews.com](https://zeitgeistnews.com) —
Vite + React + TypeScript, built to static files and served by the api container
(no Node at runtime).

- `npm run dev` — hot-reload dev server; proxies `/stats`, `/recent`, and
  `/ws/claims` to the local stack at `localhost:8000` (run `make up` first).
- `npm run build` — type-checks and emits `dist/`; the Docker build runs this
  in a node stage and copies `dist/` into the api image.
