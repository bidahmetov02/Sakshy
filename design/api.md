# API endpoints

The contract between frontend and backend. FastAPI also generates the full
OpenAPI spec at `/api/docs`. This page is the human-readable overview.

## Conventions

- Base path `/api`. JSON in and out. Timestamps are ISO 8601 UTC.
- **Auth:** an httpOnly cookie `access_token` (a JWT). Every endpoint except
  `auth/register`, `auth/login`, `/public/*` and `/health` requires it.
- **Ownership:** another user's object returns **404**, never 403.
- **Errors:** `{"detail": "..."}` from FastAPI; validation errors are 422.
- **Lists that grow** (results, incidents) use cursor pagination:
  `?limit=50&cursor=<opaque>` returns `{"items": [...], "next_cursor": "..." | null}`.

## Auth

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/auth/register` | `{email, password}` | 201 user, sets cookie |
| POST | `/auth/login` | `{email, password}` | 204, sets cookie |
| POST | `/auth/logout` | none | 204, clears cookie |
| GET | `/auth/me` | none | `{id, email}` |

## Checks

| Method | Path | Body / query | Returns | Notes |
|---|---|---|---|---|
| GET | `/checks` | `?group_id=` | list of checks with live state | **dashboard snapshot**: config + `check_state` + open incident `started_at` |
| POST | `/checks` | `{name, url, interval_s, timeout_s, expected_status, expected_body?, group_id?, alert_emails[]}` | 201 check | `alert_emails` required; the UI pre-fills the account email. Used only while ungrouped: a group's list replaces it. Responses include `effective_alert_emails` so the UI can show who actually gets alerted. |
| GET | `/checks/{id}` | none | check with live state | |
| PATCH | `/checks/{id}` | any config fields | check | state is kept; an interval change recomputes `next_run_at` |
| DELETE | `/checks/{id}` | none | 204 | soft delete; closes an open incident (`check_deleted`) |
| POST | `/checks/{id}/pause` | none | 204 | closes an open incident (`paused`), no UP email |
| POST | `/checks/{id}/resume` | none | 204 | state `pending`, runs right away |
| POST | `/checks/{id}/run` | none | 202, or **409** if currently running | sets `next_run_at = now()` |

## History and incidents

| Method | Path | Query | Returns | Source |
|---|---|---|---|---|
| GET | `/checks/{id}/stats` | `range=day\|week\|month` | `{uptime_pct, buckets: [{t, total, ok, avg_ms, max_ms}]}`; empty buckets come back as nulls = gaps | day: raw results in 5-minute buckets (288 points); week/month: hourly rollups (168 or 720 points) |
| GET | `/checks/{id}/results` | `limit, cursor` | latest raw results | raw table, newest first |
| GET | `/incidents` | `check_id?, group_id?, from?, to?, limit, cursor` | `[{id, check_id, started_at, ended_at, duration_s, end_reason, cause}]` | incident log; `duration_s` is computed |

## Groups

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| GET | `/groups` | none | groups with **computed status** | tiered: operational / partial outage / major outage; paused checks excluded |
| POST | `/groups` | `{name, alert_emails[]}` | 201 group | `alert_emails` required, pre-filled |
| PATCH | `/groups/{id}` | `{name?, alert_emails?}` | group | |
| DELETE | `/groups/{id}` | none | 204 | member checks become ungrouped |

## Maintenance windows

| Method | Path | Body / query | Returns | Notes |
|---|---|---|---|---|
| GET | `/maintenance` | `?status=active\|upcoming\|past` | windows | |
| POST | `/maintenance` | `{check_id \| group_id, starts_at, ends_at, reason?}` | 201 window | exactly one target |
| PATCH | `/maintenance/{id}` | `{starts_at?, ends_at?, reason?}` | window | **end early** = set `ends_at` to now |
| DELETE | `/maintenance/{id}` | none | 204 | |

## Status pages (owner side)

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| GET | `/status-pages` | none | pages | |
| POST | `/status-pages` | `{title, show_response_times, show_uptime, show_incidents}` | 201 page | random slug generated |
| GET / PATCH / DELETE | `/status-pages/{id}` | flags, title, `is_enabled` | page / 204 | |
| PUT | `/status-pages/{id}/items` | `[{check_id, display_name, position}]` | items | replaces the whole list; checks only |

## Public (no auth)

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/public/{slug}` | `{title, overall_status, items: [{display_name, state, since, uptime_pct?, last_response_ms?}], incidents?}` | separate response schema: **no URLs, ids or error messages**; optional fields only if the page's flag allows them; 404 if disabled |
| GET | `/public/{slug}/stats` | `?item=<position>&range=` buckets | only if `show_uptime` or `show_response_times` |
| GET | `/public/{slug}/events` | SSE stream | only events for the page's listed checks, with the same public fields |

## Realtime

| Method | Path | Returns |
|---|---|---|
| GET | `/events` | SSE stream of the user's check events |

SSE events:
```
event: check
data: {"check_id": 17, "state": "down", "last_checked_at": "...", "last_response_ms": null,
       "incident_started_at": "..."}

event: incident
data: {"id": 9, "check_id": 17, "started_at": "...", "ended_at": null}

: ping            # comment line every 15 s, keeps the connection alive
```

## Health

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{"db": "ok", "worker_last_seen_s": 4}`, which also shows whether monitoring itself is alive |
