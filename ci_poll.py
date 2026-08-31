#!/usr/bin/env python3
"""App Store poller that runs on GitHub Actions (no laptop needed).

Each scheduled run: reads the session from Cloudflare KV, scrapes App Store
Connect locally in the Actions runner's browser (free), pushes a fresh cache
+ rotated cookies back to KV, diffs for new downloads, and pings Telegram
(new-download alert, or an hourly "no new downloads" heartbeat).

All state (cache, cookies, last-seen units, heartbeat stamp) lives in KV, so
runs are stateless and independent.

Env (GitHub secrets): CLOUDFLARE_API_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ACCOUNT_ID = "a3f6194606bb59b39263fb6b4390004f"
NAMESPACE_ID = "2c74db63a1894c86bfada94c17b01e47"
CF_TOKEN = os.environ["CLOUDFLARE_API_TOKEN"]
BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
HEARTBEAT_MIN_GAP = 58 * 60
IST = ZoneInfo("Asia/Kolkata")

KV = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{NAMESPACE_ID}/values/"

RANK_URL = ("https://appstoreconnect.apple.com/trends/gsf/salesTrendsApp/businessareas/"
            "InternetServices/subjectareas/iTunes/vcubes/778/timeseries")
PAGE = "https://appstoreconnect.apple.com/trends/sales?measure=units_utc&period=hour&interval=hour"
FETCH_JS = """
async ({url, body}) => {
  const r = await fetch(url, {method:'POST', headers:{
    'content-type':'text/plain;charset=UTF-8','csrf':'null',
    'x-requested-with':'OWASP CSRFGuard Project','uicomponentname':'table'
  }, body: JSON.stringify(body)});
  const t = await r.text();
  try { return {status:'ok', json: JSON.parse(t)}; }
  catch(e){ return {status:'bad', text: t.slice(0,120)}; }
}
"""


def kv_get(key):
    req = urllib.request.Request(KV + key, headers={"Authorization": f"Bearer {CF_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def kv_put(key, value):
    req = urllib.request.Request(KV + key, data=value.encode(),
                                 headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "text/plain"},
                                 method="PUT")
    urllib.request.urlopen(req, timeout=30).read()


def tg_send(text):
    data = urllib.parse.urlencode({
        "chat_id": CHAT, "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps({"keyboard": [[{"text": "/update"}], [{"text": "/mtd"}, {"text": "/today"}]],
                                    "resize_keyboard": True, "is_persistent": True}),
    }).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{BOT}/sendMessage", data=data), timeout=30).read()
    except Exception as e:
        print("tg_send failed:", e, file=sys.stderr)


def iso_day(d):
    return d.strftime("%Y-%m-%dT00:00:00.000Z")


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00.000Z")


def body(group, start, end, measure, interval):
    return {"measures": [{"key": measure}], "group": group, "filters": [], "cubeName": "sales",
            "interval": {"key": interval, "startDate": start, "endDate": end}, "componentName": "table",
            "cubeApiType": "TIMESERIES", "sorting": "DESC", "limit": 100, "optionalParams": {"pageId": ["ci"]}}


def valid(c):
    d = c.get("domain", "")
    return d and "%" not in d and d.lstrip(".").endswith("apple.com")


def scrape(cookies_raw):
    cookies = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                "path": "/", "secure": True} for c in cookies_raw if valid(c)]
    n = datetime.now(timezone.utc)
    month_start = iso_day(n.replace(day=1))
    hour_start = iso_day(n - timedelta(days=4))
    now_iso = iso_now()

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"))
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.goto(PAGE, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        if any(x in page.url for x in ("login", "signin", "idmsa")):
            b.close()
            return None, None

        def q(group, start, end, measure, interval):
            r = page.evaluate(FETCH_JS, {"url": RANK_URL, "body": body(group, start, end, measure, interval)})
            if r.get("status") != "ok":
                raise RuntimeError(f"query {measure}/{interval}: {r}")
            return r["json"]

        day = {"units": q(["content"], month_start, now_iso, "units_utc", "day"),
               "sales": q(["content"], month_start, now_iso, "total_tax_usd_utc", "day"),
               "proceeds": q(["content"], month_start, now_iso, "Royalty_utc", "day")}
        hour = {"units": q(["content"], hour_start, now_iso, "units_utc", "hour"),
                "sales": q(["content"], hour_start, now_iso, "total_tax_usd_utc", "hour"),
                "proceeds": q(["content"], hour_start, now_iso, "Royalty_utc", "hour")}
        fresh = ctx.cookies()
        b.close()

    last_final = "0000-00-00"
    for row in day["units"]["result"]:
        for pt in row["data"]:
            if (pt.get("units_utc") or 0) > 0 and pt.get("day", "") > last_final:
                last_final = pt["day"]
    cache = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             "dayScrape": day, "hourScrape": hour, "lastFinalizedDay": last_final}
    kv_cookies = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                   "path": c.get("path", "/"), "httpOnly": c.get("httpOnly", False),
                   "secure": c.get("secure", True)} for c in fresh if valid(c)]
    return cache, kv_cookies


def last24_window(hour_scrape):
    mx = None
    for row in hour_scrape["units"]["result"]:
        for p in row["data"]:
            if p.get("hour") and (mx is None or p["hour"] > mx):
                mx = p["hour"]
    if not mx:
        return None, None
    e = datetime.fromisoformat(mx.replace("Z", "+00:00")).timestamp()
    return e - 23 * 3600, e


def fmt_zones(h):
    d = datetime.fromisoformat(h.replace("Z", "+00:00"))
    return f"{d.astimezone(IST).strftime('%b %-d, %-I:%M %p')} IST  ({d.astimezone(timezone.utc).strftime('%-I:%M %p')} UTC)"


# Two accounts = two providers under one Apple ID, each with its own cookie jar,
# cache, and state keys in KV. Account 2 is skipped silently until its cookies
# exist (session_cookies_2), so single-account setups are unaffected.
ACCOUNTS = {
    "1": {"cookies": "session_cookies",   "cache": "scrape_cache",   "alert": "session_alert_flag",
          "state": "sales_state",   "hb": "poll_heartbeat_ts",   "label": "Account 1 · Escapade"},
    "2": {"cookies": "session_cookies_2", "cache": "scrape_cache_2", "alert": "session_alert_flag_2",
          "state": "sales_state_2", "hb": "poll_heartbeat_ts_2", "label": "Account 2 · Sunny Dasari"},
}


def scrape_and_store(acct="1"):
    """Scrape one account and push its fresh cache + rotated cookies to KV.
    Returns the cache dict, or None (no cookies yet, or session dead → alert once)."""
    cfg = ACCOUNTS[acct]
    cookies_raw = kv_get(cfg["cookies"])
    if not cookies_raw:
        return None  # account not set up — skip silently
    cache, kv_cookies = scrape(json.loads(cookies_raw))
    if cache is None:
        # Alert AT MOST ONCE per outage (flag in KV), never every cycle.
        if not kv_get(cfg["alert"]):
            tg_send(f"⚠️ <b>{cfg['label']} — session expired</b> — the poller can't fetch. "
                    "Reply here with fresh cookies for this account "
                    "(DevTools → Application → Cookies → ⌘A → ⌘C). "
                    "You won't be reminded again until it's fixed.")
            kv_put(cfg["alert"], "1")
        return None
    kv_put(cfg["alert"], "")  # session healthy → clear the outage flag
    kv_put(cfg["cache"], json.dumps(cache))
    kv_put(cfg["cookies"], json.dumps(kv_cookies))
    return cache


def compute_new_state(cache, acct="1"):
    """Return (new_state, deltas) for the last-24h window vs stored sales_state."""
    ws, we = last24_window(cache["hourScrape"])
    state = kv_get(ACCOUNTS[acct]["state"])
    state = json.loads(state) if state else {}
    new_state = {}
    deltas = []
    for row in cache["hourScrape"]["units"]["result"]:
        title = row["metadata"][0]["title"] if row["metadata"] else "(unknown)"
        for p in row["data"]:
            h = p.get("hour")
            if not h:
                continue
            t = datetime.fromisoformat(h.replace("Z", "+00:00")).timestamp()
            if ws is None or not (ws <= t <= we):
                continue
            key = f"{title}|{h}"
            u = p.get("units_utc", 0)
            if u > state.get(key, 0):
                deltas.append((title, h, u - state.get(key, 0)))
            new_state[key] = u
    return new_state, deltas


def process_deltas(cache, acct="1"):
    """Normal cycle for one account: alert on genuinely new units, else (for the
    primary account only) a throttled heartbeat."""
    cfg = ACCOUNTS[acct]
    cold = kv_get(cfg["state"]) is None
    new_state, deltas = compute_new_state(cache, acct)
    kv_put(cfg["state"], json.dumps(new_state))

    sync_ist = datetime.now(IST).strftime("%b %-d, %-I:%M %p")
    footer = f"\n\n🕒 {sync_ist} IST  (+ Apple's ~2h lag)"

    if cold:
        # First time we see this account — seed state quietly, announce once.
        tg_send(f"✅ Now watching <b>{cfg['label']}</b> for new downloads.{footer}")
        kv_put(cfg["hb"], str(time.time()))
        return
    if deltas:
        by_hour = {}
        for title, h, d in deltas:
            by_hour.setdefault(h, []).append((title, d))
        lines = [f"🆕 <b>New activity · {cfg['label']}</b>"]
        for h in sorted(by_hour):
            items = sorted(by_hour[h], key=lambda x: -x[1])
            lines.append(f"🕐 {fmt_zones(h)} — +{sum(d for _, d in items)} unit(s)")
            for title, d in items:
                lines.append(f"    +{d} — {esc(title)}")
        send_long("\n".join(lines) + footer)
    elif acct == "1":  # heartbeat only for the primary account, to limit noise
        last_hb = kv_get(cfg["hb"])
        last_hb = float(last_hb) if last_hb else 0
        if time.time() - last_hb >= HEARTBEAT_MIN_GAP:
            tg_send(f"✅ No new downloads — last 24h up to date.{footer}")
            kv_put(cfg["hb"], str(time.time()))


def esc(s):
    """Escape for Telegram parse_mode=HTML (app titles may contain & < >)."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_long(text, limit=3900):
    """Send text as one message, or split on line boundaries if over Telegram's
    ~4096-char cap."""
    buf = ""
    for ln in text.split("\n"):
        if buf and len(buf) + len(ln) + 1 > limit:
            tg_send(buf)
            buf = ""
        buf = f"{buf}\n{ln}" if buf else ln
    if buf:
        tg_send(buf)


def render_account_block(label, cache):
    """One account's last-24h section: By-App digest + By-Hour timeline.
    Returns (text, total_units)."""
    hour = cache["hourScrape"]
    ws, we = last24_window(hour)
    per_hour = {}   # hour_iso -> list[(title, units)]
    per_app = {}    # title -> total units
    total = 0
    for row in hour["units"]["result"]:
        title = row["metadata"][0]["title"] if row["metadata"] else "(unknown)"
        for p in row["data"]:
            h = p.get("hour")
            if not h:
                continue
            t = datetime.fromisoformat(h.replace("Z", "+00:00")).timestamp()
            if ws is None or not (ws <= t <= we):
                continue
            u = p.get("units_utc", 0) or 0
            if u <= 0:
                continue
            per_hour.setdefault(h, []).append((title, u))
            per_app[title] = per_app.get(title, 0) + u
            total += u

    lines = [f"━━━━━  <b>{label}</b>  ━━━━━"]
    if total == 0:
        lines.append("📊 No sales in the last 24h.")
        return "\n".join(lines), 0

    lines.append(f"📊 <b>{total} units</b> · {len(per_app)} apps · last 24h")
    lines.append("🏆 <b>By app</b>")
    for title, u in sorted(per_app.items(), key=lambda x: (-x[1], x[0].lower())):
        lines.append(f"   <b>{u}×</b>  {esc(title)}")

    lines.append("🕐 <b>By hour</b>")
    cur_day = None
    for h in sorted(per_hour):
        d = datetime.fromisoformat(h.replace("Z", "+00:00"))
        d_ist = d.astimezone(IST)
        day_label = d_ist.strftime("%a, %b %-d")
        if day_label != cur_day:
            cur_day = day_label
            lines.append(f"📅 <b>{day_label}</b>")
        ist_t = d_ist.strftime("%-I:%M %p")
        utc_t = d.astimezone(timezone.utc).strftime("%-I:%M %p")
        items = sorted(per_hour[h], key=lambda x: -x[1])
        hsum = sum(u for _, u in items)
        lines.append(f"<b>{ist_t}</b> · {utc_t} UTC — {hsum}")
        for title, u in items:
            prefix = f"{u}× " if u > 1 else ""
            lines.append(f"   • {prefix}{esc(title)}")
    return "\n".join(lines), total


def send_refresh_report(accounts):
    """On-demand /update reply. `accounts` = list of (label, cache) — one section
    per account, stamped 'now'."""
    now_ist = datetime.now(IST).strftime("%b %-d, %-I:%M %p")
    parts = [f"✅ <b>Refreshed</b> · {now_ist} IST"]
    for label, cache in accounts:
        block, _ = render_account_block(label, cache)
        parts.append("\n" + block)
    parts.append("\n<i>+ Apple's ~2h lag</i>")
    send_long("\n".join(parts))


def handle_refresh():
    """Service an on-demand /update: scrape every configured account, post one
    combined report, and sync each account's sales_state so the next normal
    cycle doesn't re-alert the same units."""
    kv_put("refresh_request", "")     # claim both flags immediately
    kv_put("refresh_request_2", "")
    accounts = []
    for acct in ("1", "2"):
        cache = scrape_and_store(acct)
        if cache is not None:
            new_state, _ = compute_new_state(cache, acct)
            kv_put(ACCOUNTS[acct]["state"], json.dumps(new_state))
            accounts.append((ACCOUNTS[acct]["label"], cache))
    if not accounts:
        tg_send("⚠️ Couldn't refresh — sessions may have expired.")
        return
    send_refresh_report(accounts)


def main():
    """One normal cycle for every configured account (scrape + diff/heartbeat)."""
    for acct in ("1", "2"):
        cache = scrape_and_store(acct)
        if cache is not None:
            process_deltas(cache, acct)


def loop():
    """Long-running loop for GitHub Actions. Every ~20s it checks the KV refresh
    flags (set by the Worker's /update button) and services them immediately;
    otherwise it runs a normal cycle every 30 min. Exits after ~5.5h so the
    workflow's schedule/concurrency relaunches a fresh loop."""
    end = time.time() + 19800          # ~5.5 hours (under GitHub's 6h job cap)
    next_scrape = 0.0                  # 0 → force an immediate first scrape
    print("ci_poll loop started", flush=True)
    while time.time() < end:
        try:
            if kv_get("refresh_request") or kv_get("refresh_request_2"):  # /update tapped
                print("on-demand refresh requested", flush=True)
                handle_refresh()
                next_scrape = time.time() + 1800
            elif time.time() >= next_scrape:
                print(f"scheduled poll at {datetime.now(timezone.utc)}", flush=True)
                main()
                next_scrape = time.time() + 1800
        except Exception as e:
            print("loop iteration error:", e, file=sys.stderr, flush=True)
        time.sleep(20)
    print("loop finished; workflow relaunches a fresh one", flush=True)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    elif "--refresh" in sys.argv:
        handle_refresh()
    else:
        main()
