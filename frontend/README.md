# Sakshy frontend

React + TypeScript SPA, served by nginx in compose (`http://localhost:8080`), which also proxies `/api`.

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server; proxies `/api` to `localhost:8000` (the compose api) |
| `npm test` | Vitest unit tests (run in UTC) |
| `npm run lint` / `npm run typecheck` | oxlint / `tsc -b` |
| `npm run gen:api` | Regenerates `src/api/schema.d.ts` from the running API's OpenAPI spec. Runs openapi-typescript with its own TypeScript 5 via `npx`, since the project uses TypeScript 7 |

Layout: `src/api` (typed client and TanStack Query hooks), `src/lib` (pure logic, unit-tested),
`src/components`, `src/pages`, `src/styles` (`tokens.css` holds the light and dark palettes).
