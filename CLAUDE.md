# Sakshy

Website uptime checker/monitor: users define sites, a worker checks them, alerts on down/up.

- Design (draft, source of truth): `design/` — start with `architecture.md`.
- Backend: Python 3.14, FastAPI, SQLAlchemy, Pydantic, pytest. DB: PostgreSQL 18.
- Frontend: React + TypeScript. Email: Mailpit.
- Runs locally via Docker. Ignore deployment until the user raises it.
- `backend/` is a bare scaffold.

## Rules
- Consult the user on every decision before committing to it. Don't decide unilaterally.
- For critical design decisions, present alternatives with trade-offs and a recommendation.
- Critique the user's decisions honestly; push back when a better option exists.
- Keep this file as short as possible.
