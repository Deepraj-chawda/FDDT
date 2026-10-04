# FDDT — Fraud Document Detection Tool — Frontend

React + TypeScript (Vite), shadcn-style UI components (Radix + Tailwind),
TanStack Query, React Hook Form + Zod, React Router, react-pdf. See
[`../docs/frontend/screens.md`](../docs/frontend/screens.md) and
[`../docs/frontend/components.md`](../docs/frontend/components.md) for the
screens and shared components.

**Screens:** login, dashboard, case queue, my cases, new case / upload, case
detail (live processing status, PDF overlays, risk reasons, audit timeline,
report export), audit history, and admin settings (issuer registry, risk
rules, users).

## Setup

The backend (`../backend`) must be running first — see the root README.
Postgres/Redis: `docker compose up -d` from the repo root.

```bash
npm install
npm run dev
```

Open http://localhost:5173. The dev server proxies `/api/*` to
`http://127.0.0.1:8000` (see `vite.config.ts`), so no backend CORS setup
is needed locally.

Seeded test login (from `backend/seed.py`): `admin@example.com` /
`ChangeMe123!`.

## Scripts

- `npm run dev` — dev server with HMR
- `npm run build` — type-check (`tsc -b`) + production build
- `npm run lint` — oxlint
