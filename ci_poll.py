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


def main():
    cookies_raw = kv_get("session_cookies")
    if not cookies_raw:
        print("no session_cookies in KV")
        return
    cache, kv_cookies = scrape(json.loads(cookies_raw))
    if cache is None:
        tg_send("⚠️ <b>App Store session expired</b> — the cloud poller can't fetch. "
                "Reply here with fresh cookies (DevTools → Application → Cookies → ⌘A → ⌘C).")
        return

    kv_put("scrape_cache", json.dumps(cache))
    kv_put("session_cookies", json.dumps(kv_cookies))

    ws, we = last24_window(cache["hourScrape"])
    state = kv_get("sales_state")
    state = json.loads(state) if state else None
    cold = state is None
    state = state or {}
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
    kv_put("sales_state", json.dumps(new_state))

    now_ist = datetime.now(IST).strftime("%-I:%M %p")
    if cold:
        tg_send(f"✅ {now_ist} IST — cloud poller started. Watching for new downloads.")
        kv_put("poll_heartbeat_ts", str(time.time()))
        return
    if deltas:
        by_hour = {}
        for title, h, d in deltas:
            by_hour.setdefault(h, []).append((title, d))
        lines = ["🆕 <b>New App Store activity</b>"]
        for h in sorted(by_hour):
            items = sorted(by_hour[h], key=lambda x: -x[1])
            lines.append(f"🕐 {fmt_zones(h)} — +{sum(d for _, d in items)} unit(s)")
            for title, d in items:
                lines.append(f"    +{d} — {title}")
        tg_send("\n".join(lines))
    else:
        last_hb = kv_get("poll_heartbeat_ts")
        last_hb = float(last_hb) if last_hb else 0
        if time.time() - last_hb >= HEARTBEAT_MIN_GAP:
            tg_send(f"✅ {now_ist} IST — no new downloads (cloud poller running, last 24h up to date)")
            kv_put("poll_heartbeat_ts", str(time.time()))


if __name__ == "__main__":
    main()
