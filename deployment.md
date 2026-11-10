# Deployment

## Branches

- `master`: the app as developed and run locally (Mailpit, emulator, dev ports).
- `prod`: `master` plus what production needs: Gmail SMTP login, Telegram alerts and
  operator notifications, `docker-compose.prod.yml`, `deploy/Caddyfile`, `.env.example`.
  The server runs this branch.

To ship work from `master`: `git checkout prod && git merge master && git push`, then
update the server (below).

## How it runs

One Ubuntu VPS, everything in Docker. Caddy is the only thing reachable from outside
(80, 443): it gets the Let's Encrypt certificate for the DuckDNS name and proxies to the
frontend's nginx, which serves the SPA and proxies `/api`. Database, API and worker ports
stay closed; Mailpit and the emulator don't start.

`COMPOSE_FILE` in `.env` adds `docker-compose.prod.yml`, so plain `docker compose ...` on the
server is always production.

## Update

```sh
git pull && docker compose up -d --build
docker compose restart frontend     # nginx keeps the old API address otherwise (502)
```

## Alerts

- **Email** goes out from the Gmail account (about 500 a day). Revoking the app password at
  myaccount.google.com/apppasswords cuts Sakshy off without touching anything else.
- **Telegram**: every check on the server alerts the operator's chat, through the outbox
  like emails (retried for about an hour). The operator's checks ring and link to the check;
  other users' arrive silently with the owner's email. Chat id: press Start in the bot, then
  `curl https://api.telegram.org/bot<TOKEN>/getUpdates`.
- **Operator notifications** (sign-ups are open and emails go out from the operator's
  account): the API messages the operator when someone else signs up, adds a check, changes
  a check's URL or recipients, or changes the alert list of a group with checks. One try,
  no retry.

## Operations

```sh
docker compose logs worker | grep -i -E "alert|smtp|telegram"     # delivery problems
docker compose exec -T db pg_dump -U sakshy -Fc sakshy > sakshy-$(date +%F).dump  # backup; copy off the server
docker compose exec db psql -U sakshy -c "DELETE FROM users WHERE lower(email) = lower('x@y.z')"  # remove an abuser and all they own
```
