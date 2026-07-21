# asc-poll — App Store Connect poller on GitHub Actions

Runs every ~10 min with no laptop needed. Each run scrapes App Store Connect
in the Actions runner, refreshes the Cloudflare KV cache (that the Telegram
bot serves), and pings Telegram on new downloads (or an hourly heartbeat).

## Secrets required (Settings → Secrets and variables → Actions)
- `CLOUDFLARE_API_TOKEN` — token with **Workers KV Storage: Edit** on the account
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Account/namespace IDs are hardcoded in `ci_poll.py` (identifiers, not secrets).

## Notes
- Repo is public so Actions minutes are unlimited. No secrets are committed.
- If Apple's session expires, the bot asks for fresh cookies; paste them to the
  bot and the worker updates KV — the next run picks them up automatically.
