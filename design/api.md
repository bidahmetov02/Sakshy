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
  `?limit=50&cursor=<opaque>` (limit 1–200) returns `{"items": [...], "next_cursor": "..." | null}`.
- **Partial updates (PATCH):** a field left out is kept; an explicit null is a 422,
  except where null clears a value (`expected_body`, `group_id`, a window's `reason`).

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
| GET | `/checks` | `?group_id=` | list of checks with live state | **dashboard snapshot**: config + `check_state` + open incident `started_at` + `in_maintenance` (an active window targets the check or its group) |
| POST | `/checks` | `{name, url, interval_s, timeout_s, expected_status, expected_body?, group_id?, alert_emails[]}` | 201 check | `timeout_s` at most 30 s (bounds the time to confirm DOWN, see `state-machine.md`). `alert_emails` required; the UI pre-fills the account email. Used only while ungrouped: a group's list replaces it. Responses include `effective_alert_emails` so the UI can show who actually gets alerted. A `group_id` that isn't the user's is 422. |
| GET | `/checks/{id}` | none | check with live state | |
| PATCH | `/checks/{id}` | any config fields | check | state is kept; an interval change shifts `next_run_at` by the difference (`max(now, next_run_at − old + new)`, so the schedule stays anchored), except while a run holds the lease or a retry streak is under way |
| DELETE | `/checks/{id}` | none | 204 | soft delete; closes an open incident (`check_deleted`) |
| POST | `/checks/{id}/pause` | none | 204 | closes an open incident (`paused`), no UP email; idempotent |
| POST | `/checks/{id}/resume` | none | 204 | state `pending`, runs right away; idempotent |
| POST | `/checks/{id}/run` | none | 202, or **409** if currently running or paused | sets `next_run_at = now()` |

## History and incidents

| Method | Path | Query | Returns | Source |
|---|---|---|---|---|
| GET | `/checks/{id}/stats` | `range=day\|week\|month` (default day) | `{uptime_pct, buckets: [{t, total, ok, avg_ms, max_ms}]}`; empty buckets come back as nulls = gaps | day: raw results in 5-minute buckets (288 points); week/month: hourly rollups (168 or 720 points) |
| GET | `/checks/{id}/results` | `limit, cursor` | `[{checked_at, ok, response_ms, status_code, error_kind, error_message, is_retry}]` | raw table, newest first, retries included (flagged) |
| GET | `/incidents` | `check_id?, group_id?, from?, to?, limit, cursor` | `[{id, check_id, started_at, detected_at, ended_at, duration_s, end_reason, cause_kind, cause_message, began_in_maintenance, alerted}]` | incident log, newest first; `duration_s` is computed, null while open; `alerted` = a DOWN email was queued |

**Stats:** buckets are aligned to UTC; the last one is the current, unfinished bucket.
Retries are never counted (the hourly rollups skip them, and the day view does
too), so a burst of retries doesn't skew the chart. `uptime_pct` is `ok / total`
over the range's buckets, null with no runs. `avg_ms` and `max_ms` cover only the
runs that got a response.

**Incidents:** `from` and `to` (timezone required) select the incidents that
*overlap* the period. `group_id` means the checks in that group now. Incidents of
deleted checks are left out.

## Groups

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| GET | `/groups` | none | groups with **computed status**, `check_count`, `down_count` | ordered by name |
| GET | `/groups/{id}` | none | group | |
| POST | `/groups` | `{name, alert_emails[]}` | 201 group, **409** if the name is taken | `alert_emails` required, pre-filled |
| PATCH | `/groups/{id}` | `{name?, alert_emails?}` | group, **409** if the name is taken | a new list applies to emails queued from then on |
| DELETE | `/groups/{id}` | none | 204 | member checks become ungrouped; the group's maintenance windows are deleted, so a DOWN email they held back goes out (to each check's own list) |

**Status** (groups and public pages alike): over the checks that count, i.e. not
paused and not in maintenance. **operational** if none is down, **major outage** if
all are, **partial outage** otherwise. Only `down` counts: `failing` isn't confirmed
yet. With no check that counts, the status is operational.

## Maintenance windows

| Method | Path | Body / query | Returns | Notes |
|---|---|---|---|---|
| GET | `/maintenance` | `?status=active\|upcoming\|past` | windows, each with its `status` | newest first |
| POST | `/maintenance` | `{check_id \| group_id, starts_at, ends_at, reason?}` | 201 window | exactly one target, the user's own live check or group (else 422) |
| PATCH | `/maintenance/{id}` | `{starts_at?, ends_at?, reason?}` | window, **409** if it has ended | **end early** = set `ends_at` to now; `starts_at` only before the window starts (else 409) |
| DELETE | `/maintenance/{id}` | none | 204 | any window, past ones included |

**Times:** checked on the database clock, and timezone required. A time in the
past means **now**: a `starts_at` on create (or while the window is upcoming) starts
it now, and an `ends_at` on an active window ends it now. So a browser whose clock
runs a little behind still gets "start now" or "end now". `ends_at` must end up
after `starts_at`, so a new window must end in the future. Windows of deleted
checks are hidden (404).

## Status pages (owner side)

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| GET | `/status-pages` | none | pages, each with its `items` | |
| POST | `/status-pages` | `{title, show_response_times?, show_uptime?, show_incidents?}` | 201 page | random 12-character slug; flags default to false / true / false |
| GET / PATCH / DELETE | `/status-pages/{id}` | flags, title, `is_enabled` | page / 204 | |
| PUT | `/status-pages/{id}/items` | `[{check_id, display_name, position}]` (max 100) | items `[{id, check_id, display_name, position}]` | replaces the whole list, so items get new ids; checks only, the user's own and live, each once (else 422); sorted by `position`, ties by id. Items of deleted checks are hidden |

## Public (no auth)

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/public/{slug}` | `{title, overall_status, items: [{id, display_name, state, since, uptime_pct?, last_response_ms?}], incidents?: [{item_id, display_name, started_at, ended_at}]}` | separate response schema: **no URLs, check ids or error messages**; items are named by their item `id`; optional fields are left out unless the page's flag allows them; 404 if disabled or unknown |
| GET | `/public/{slug}/stats` | `?item=<item id>&range=` | stats as above, with only the fields the flags allow (`total`, `ok`, `uptime_pct` for `show_uptime`; `avg_ms`, `max_ms` for `show_response_times`); 404 if neither flag is on |

**Public item state:** `paused`, else `maintenance` (it wins over down), else
`down`, else `up` (`failing` shows as up: unconfirmed, no incident), else
`pending`. `since` is the incident's start when down, the earliest active window's
start in maintenance, otherwise when the state last changed. `uptime_pct` covers
the last 30 days. `incidents` are the latest 20 of the listed checks that overlap
the last 30 days, without their cause. Deleted checks are left out.
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
| GET | `/health` | `{"db": "ok", "worker_last_seen_s": 4}`, which also shows whether monitoring itself is alive. `worker_last_seen_s` is null with no worker heartbeat; 503 with `"db": "error"` if the database is unreachable |
