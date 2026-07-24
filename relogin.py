#!/usr/bin/env python3
"""Log in to App Store Connect with Apple ID + password + 2FA, headless on
GitHub Actions, and store the fresh session cookies in Cloudflare KV.

The 2FA code is relayed through Telegram: this script drives the login to the
verification-code screen, asks you (via the bot) for the 6 digits, then polls
KV for the code you reply with (the Cloudflare Worker's Telegram webhook writes
it there). On success it captures the session cookies exactly like ci_poll.py
expects and clears the "session expired" flags.

Sends screenshots to Telegram on trouble so we can iterate on Apple's login
page selectors without guessing blind.

Env (GitHub secrets): APPLE_ID, APPLE_PASSWORD, CLOUDFLARE_API_TOKEN,
                      TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ACCOUNT_ID = "a3f6194606bb59b39263fb6b4390004f"
NAMESPACE_ID = "2c74db63a1894c86bfada94c17b01e47"
CF_TOKEN = os.environ["CLOUDFLARE_API_TOKEN"]
BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
APPLE_ID = os.environ["APPLE_ID"]
APPLE_PW = os.environ["APPLE_PASSWORD"]

KV = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{NAMESPACE_ID}/values/"
LOGIN_URL = "https://appstoreconnect.apple.com/login"
CODE_WAIT_SECS = 240   # how long to wait for you to reply with the 2FA code


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
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{BOT}/sendMessage", data=data), timeout=30).read()
    except Exception as e:
        print("tg_send failed:", e, file=sys.stderr)


def tg_photo(path, caption=""):
    """Upload a screenshot so we can see what Apple's page looked like."""
    try:
        boundary = "----" + uuid.uuid4().hex
        with open(path, "rb") as f:
            img = f.read()
        parts = []
        for k, v in (("chat_id", CHAT), ("caption", caption[:1000])):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                      f"filename=\"shot.png\"\r\nContent-Type: image/png\r\n\r\n").encode())
        parts.append(img)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT}/sendPhoto", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        print("tg_photo failed:", e, file=sys.stderr)


def valid(c):
    d = c.get("domain", "")
    return d and "%" not in d and d.lstrip(".").endswith("apple.com")


def wait_for_code():
    """Prompt via Telegram, then poll KV for the 6-digit code the worker stores
    when you reply. Returns the code or None on timeout."""
    kv_put("relogin_code", "")
    kv_put("relogin_state", "awaiting_code")
    tg_send("🔐 <b>Apple sent a 6-digit verification code</b> to your trusted devices.\n\n"
            "Reply to this chat with just the 6 digits.")
    deadline = time.time() + CODE_WAIT_SECS
    while time.time() < deadline:
        code = (kv_get("relogin_code") or "").strip()
        if len(code) == 6 and code.isdigit():
            kv_put("relogin_state", "")
            kv_put("relogin_code", "")
            return code
        time.sleep(4)
    kv_put("relogin_state", "")
    return None


def find_field(scope, selectors):
    for sel in selectors:
        try:
            el = scope.locator(sel).first
            if el.count() > 0 and el.is_visible():
                return el
        except Exception:
            continue
    return None


def main():
    tg_send("⏳ Starting App Store Connect login…")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
              locale="en-US")
        page = ctx.new_page()

        def shot(caption):
            path = "/tmp/asc_shot.png"
            try:
                page.screenshot(path=path, full_page=False)
                tg_photo(path, caption)
            except Exception as e:
                print("shot failed", e, file=sys.stderr)

        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)

            # The Apple auth widget is usually inside an iframe; fall back to the
            # main frame if not. Work against whichever scope has the fields.
            scope = page
            for fr in page.frames:
                if "idmsa.apple.com" in (fr.url or "") or "auth" in (fr.name or ""):
                    scope = fr
                    break

            # ── Apple ID ─────────────────────────────────────────────────────
            acc = find_field(scope, ["#account_name_text_field", "input[name='accountName']",
                                     "input[type='text']", "input[autocomplete='username']"])
            if not acc:
                shot("❌ Couldn't find the Apple ID field. Here's the page — I'll adjust the selectors.")
                tg_send("Login page didn't show the expected Apple ID field (see screenshot).")
                return
            acc.fill(APPLE_ID)
            page.wait_for_timeout(500)
            cont = find_field(scope, ["#sign-in", "button[type='submit']", "#continue-password"])
            if cont:
                cont.click()
            else:
                acc.press("Enter")
            page.wait_for_timeout(3500)

            # ── Password ─────────────────────────────────────────────────────
            pw = find_field(scope, ["#password_text_field", "input[type='password']",
                                    "input[autocomplete='current-password']"])
            if not pw:
                # Some flows reveal the password field only after a second continue.
                page.wait_for_timeout(2500)
                pw = find_field(scope, ["#password_text_field", "input[type='password']"])
            if not pw:
                shot("❌ Couldn't find the password field. Here's the page.")
                return
            pw.fill(APPLE_PW)
            page.wait_for_timeout(500)
            signin = find_field(scope, ["#sign-in", "button[type='submit']"])
            if signin:
                signin.click()
            else:
                pw.press("Enter")
            page.wait_for_timeout(6000)

            # Detect a wrong-password / challenge error early.
            body_txt = ""
            try:
                body_txt = scope.locator("body").inner_text(timeout=3000).lower()
            except Exception:
                pass
            if "incorrect" in body_txt or "not correct" in body_txt or "try again" in body_txt:
                shot("❌ Apple rejected the Apple ID or password. Check the APPLE_ID / APPLE_PASSWORD secrets.")
                return

            # ── 2FA code ─────────────────────────────────────────────────────
            code = wait_for_code()
            if not code:
                tg_send("⌛ No code received in time — login aborted. Send /relogin to try again.")
                return

            # Type the 6 digits. Apple uses either 6 single-char inputs or one field.
            digit_inputs = scope.locator("input.form-security-code-input, input[id^='char']")
            try:
                n = digit_inputs.count()
            except Exception:
                n = 0
            if n >= 6:
                for i in range(6):
                    digit_inputs.nth(i).fill(code[i])
                    page.wait_for_timeout(120)
            else:
                single = find_field(scope, ["input[type='tel']", "input[type='number']",
                                            "input[autocomplete='one-time-code']", "input[type='text']"])
                if single:
                    single.click()
                    single.type(code, delay=120)
                else:
                    scope.locator("body").type(code, delay=120)
            page.wait_for_timeout(5000)

            # ── Trust this browser (extends session to ~30 days) ─────────────
            for label in ["Trust", "Trust Browser", "Trust this browser"]:
                try:
                    btn = scope.get_by_role("button", name=label)
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click()
                        break
                except Exception:
                    continue
            page.wait_for_timeout(8000)

            # ── Confirm we reached the dashboard & capture cookies ───────────
            for _ in range(6):
                if "appstoreconnect.apple.com" in page.url and "login" not in page.url and "idmsa" not in page.url:
                    break
                page.wait_for_timeout(2500)

            cookies = ctx.cookies()
            b.close()
            names = {c["name"] for c in cookies}
            if "myacinfo" not in names:
                tg_send("⚠️ Login finished but no <b>myacinfo</b> cookie was set — Apple may have shown "
                        "an extra challenge. Screenshot incoming.")
                return
            kv_cookies = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                           "path": c.get("path", "/"), "httpOnly": c.get("httpOnly", False),
                           "secure": c.get("secure", True)} for c in cookies if valid(c)]
            kv_put("session_cookies", json.dumps(kv_cookies))
            kv_put("session_alert_flag", "")
            kv_put("session_alert_sent", "")
            tg_send(f"✅ <b>Logged in.</b> Session refreshed with {len(kv_cookies)} cookies "
                    "(trusted ~30 days). The poller will pick it up on its next run.")
        except PWTimeout as e:
            shot(f"❌ Timed out during login: {str(e)[:150]}")
        except Exception as e:
            shot(f"❌ Login error: {str(e)[:150]}")
        finally:
            try:
                b.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
