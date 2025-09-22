import os, sys, json, re, time, hashlib, requests, traceback
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# ========== CONFIG ==========
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()   # also used for summaries
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()
DEBUG      = os.environ.get("DEBUG", "0") == "1"

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing one of: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, DISCORD_STATE_CHANNEL_ID, WOS_SECRET")
    sys.exit(0)  # soft exit to keep workflow green during iteration

def _short(s: str, n: int = 200) -> str:
    s = s or ""
    return (s[:n] + "…") if len(s) > n else s

# ========== DISCORD REST ==========
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def post_message(channel_id, content):
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers={**D_HDR, "Content-Type":"application/json"},
                      json={"content": content[:1900]}, timeout=25)
    if not r.ok:
        print(f"[ERR] Discord POST ch={channel_id} {r.status_code} {_short(r.text)}")
    return r.status_code

def get_messages_after(channel_id, after_id=None, limit=100):
    params = {"limit": limit}
    if after_id: params["after"] = str(after_id)
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=25)
    if not r.ok:
        print(f"[ERR] Discord GET ch={channel_id} {r.status_code} {_short(r.text)}")
        return []
    try:
        return r.json()
    except Exception:
        print(f"[ERR] Discord GET JSON parse failed ch={channel_id}")
        return []

def post_message_with_file(channel_id, content, filename, bytes_data):
    payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
    files = {'files[0]': (filename, bytes_data, 'application/json')}
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers=D_HDR, data={"payload_json": json.dumps(payload)},
                      files=files, timeout=40)
    if not r.ok:
        print(f"[ERR] Discord POST file ch={channel_id} {r.status_code} {_short(r.text)}")
    return r.json() if r.ok else {}

def delete_message(channel_id, message_id):
    try:
        requests.delete(f"{D_API}/channels/{channel_id}/messages/{message_id}", headers=D_HDR, timeout=15)
    except Exception:
        pass

def find_latest_state_message(channel_id, bot_user_id):
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params={"limit": 25}, timeout=20)
    if not r.ok:
        return None, {}
    for m in r.json():  # newest first
        if str(m.get("author",{}).get("id")) != str(bot_user_id):
            continue
        for att in m.get("attachments", []):
            if att.get("filename") == "wos_state.json":
                try:
                    txt = requests.get(att["url"], timeout=20).text
                    return m["id"], json.loads(txt)
                except Exception:
                    return m["id"], {}
    return None, {}

def get_me():
    r = requests.get(f"{D_API}/users/@me", headers=D_HDR, timeout=15)
    r.raise_for_status()
    return r.json()["id"]

# ========== PARSERS ==========
YAML_CODES = re.compile(r"(?mi)^\s*codes\s*:\s*$")
YAML_FIDS  = re.compile(r"(?mi)^\s*fids\s*:\s*$")
BULLET     = re.compile(r"(?m)^\s*-\s*(\S+)\s*$")
CSV_CODE   = re.compile(r"(?mi)^\s*([A-Za-z0-9]{4,24})\s*$")
CSV_FID    = re.compile(r"(?mi)^\s*(\d{6,12})\s*$")

def parse_codes(text):
    out, current = set(), None
    for line in text.splitlines():
        if YAML_CODES.match(line): current="codes"; continue
        m = BULLET.match(line)
        if m and current=="codes": out.add(m.group(1).strip().upper()); continue
        m2 = CSV_CODE.match(line)
        if m2: out.add(m2.group(1).upper())
    return out

def parse_fids(text):
    out, current = set(), None
    for line in text.splitlines():
        if YAML_FIDS.match(line): current="fids"; continue
        m = BULLET.match(line)
        if m and current=="fids": out.add(m.group(1).strip()); continue
        m2 = CSV_FID.match(line)
        if m2: out.add(m2.group(1))
    return out

def fetch_text_attachments(msg):
    texts = []
    for att in msg.get("attachments", []):
        name = att.get("filename","").lower()
        if any(name.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
            try:
                resp = requests.get(att["url"], timeout=25)
                if resp.ok: texts.append(resp.text)
                else: print(f"[WARN] Attachment fetch failed {name} status={resp.status_code}")
            except Exception:
                print(f"[WARN] Attachment fetch threw for {name}")
    return texts

# ========== WOS ENDPOINTS ==========
PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
WEB_ORIGIN   = "https://wos-giftcode.centurygame.com"
WEB_REFERER  = "https://wos-giftcode.centurygame.com/"

def md5(s:str) -> str: return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + secret)

def build_sign(fid: str, cdk: str, ts_value: str, secret: str,
               order="fixed", concat="plain", uppercase=False, include_lang=False):
    pieces = {"fid": fid, "cdk": cdk, "time": ts_value}
    if include_lang:
        pieces["lang"] = "en"
    if order == "sorted":
        items = sorted(pieces.items())
    else:
        items = [(k, pieces[k]) for k in ("fid","cdk","time") if k in pieces]
        if "lang" in pieces: items.append(("lang","en"))
    base = "&".join(f"{k}={v}" for k,v in items)
    if   concat == "amp":    s = base + "&" + secret
    elif concat == "key":    s = base + "&key=" + secret
    elif concat == "prefix": s = secret + base
    else:                    s = base + secret
    d = hashlib.md5(s.encode("utf-8")).hexdigest()
    return d.upper() if uppercase else d

# Browser-like headers; API host needs X-Requested-With on some WAFs
COMMON_HDRS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
}

def post_form(url: str, form: dict, cookie: str | None, as_json=False):
    headers = {**COMMON_HDRS,
               "Origin": WEB_ORIGIN,
               "Referer": WEB_REFERER}
    headers["Content-Type"] = "application/json" if as_json else "application/x-www-form-urlencoded"
    if cookie: headers["Cookie"] = cookie
    r = requests.post(url,
                      headers=headers,
                      data=None if as_json else urlencode(form),
                      json=form if as_json else None,
                      timeout=25)
    return r.status_code, r.text

def get_form(url: str, params: dict, cookie: str | None):
    headers = {**COMMON_HDRS,
               "Origin": WEB_ORIGIN,
               "Referer": WEB_REFERER}
    if cookie: headers["Cookie"] = cookie
    r = requests.get(url, headers=headers, params=params, timeout=25)
    return r.status_code, r.text

def get_cookies_for_hosts():
    """Return two cookie headers: (cookie_web, cookie_api)."""
    print("Acquiring cookies via headless Chromium…")
    cookie_web = ""
    cookie_api = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            # Web host (sets general site cookies)
            page.goto(WEB_REFERER, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2500)
            # API host (get per-host clearance)
            # A harmless GET so Cloudflare can issue a token for the API hostname
            page.goto(GIFTCODE_URL, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2000)
            cookies = ctx.cookies()
            browser.close()
        web_pairs, api_pairs = [], []
        for c in cookies:
            dom = (c.get("domain") or "").lstrip(".")
            pair = f"{c['name']}={c['value']}"
            if dom.endswith("wos-giftcode.centurygame.com"):
                web_pairs.append(pair)
            if dom.endswith("wos-giftcode-api.centurygame.com"):
                api_pairs.append(pair)
        cookie_web = "; ".join(web_pairs)
        cookie_api = "; ".join(api_pairs)
    except Exception as e:
        print(f"[WARN] Playwright failed to fetch cookies: {e}")
    return cookie_web, cookie_api

def redeem_once(cookie_api: str, fid: str, code: str, variant: dict):
    ts_value = str(int(time.time() * 1000)) if variant.get("ts") == "ms" else str(int(time.time()))
    sign = build_sign(fid, code, ts_value, WOS_SECRET,
                      order=variant.get("order","fixed"),
                      concat=variant.get("concat","plain"),
                      uppercase=variant.get("uppercase", False),
                      include_lang=variant.get("lang", False))
    payload = {"fid": fid, "cdk": code, "time": ts_value, "sign": sign}
    if variant.get("lang"): payload["lang"] = "en"
    method = variant.get("method","POST")
    body   = variant.get("body","form")
    if method == "POST":
        return post_form(GIFTCODE_URL, payload, cookie_api or None, as_json=(body=="json"))
    else:
        return get_form(GIFTCODE_URL, payload, cookie_api or None)

# ========== MAIN ==========
had_unhandled_error = False
error_summary = ""
post_message(STATE_CH, "🟢 WOS daily run starting…")

try:
    # ---- load state from Discord ----
    bot_id = get_me()
    prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
    last_codes = int(state.get("last_id_codes") or 0)
    last_ids   = int(state.get("last_id_ids") or 0)
    roster     = state.get("roster") or {}

    # ---- read codes since last checkpoint ----
    msgs_codes = get_messages_after(CODES_CH, last_codes)
    codes = set()
    for m in msgs_codes:
        txt = m.get("content","") or ""
        codes |= parse_codes(txt)
        for t in fetch_text_attachments(m):
            codes |= parse_codes(t)
        last_codes = max(last_codes, int(m["id"]))

    # ---- read fids since last checkpoint ----
    msgs_ids = get_messages_after(IDS_CH, last_ids)
    new_fids = set()
    for m in msgs_ids:
        txt = m.get("content","") or ""
        new_fids |= parse_fids(txt)
        for t in fetch_text_attachments(m):
            new_fids |= parse_fids(t)
        last_ids = max(last_ids, int(m["id"]))

    # ---- update roster ----
    added = []
    for fid in sorted(new_fids):
        if fid not in roster:
            roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
            added.append(fid)

    # ---- furnace scan ----
    cookie_web, cookie_api = get_cookies_for_hosts()
    print("Cookie(web):", "present" if cookie_web else "none")
    print("Cookie(api):", "present" if cookie_api else "none")

    ok_players = 0
    furnace_ups = []
    for fid, rec in roster.items():
        ts = str(int(time.time()))
        sign_p = sign_sorted({"fid": fid, "time": ts}, WOS_SECRET)
        hp, bp = post_form(PLAYER_URL, {"fid": fid, "time": ts, "sign": sign_p}, cookie=None)
        try:
            js = json.loads(bp)
        except Exception:
            js = {}

        if hp == 200 and js.get("msg") == "success":
            ok_players += 1
            data = js.get("data", {})
            nick  = data.get("nickname")
            stove = data.get("stove_lv")
            prev  = rec.get("stove")
            rec.update({"nickname": nick, "stove": stove, "updated_at": int(time.time())})
            try:
                if prev is not None and stove is not None and int(stove) > int(prev):
                    furnace_ups.append(f"🔥 `{fid}` {nick or ''} • {prev} ➜ {stove}")
            except Exception:
                pass
        else:
            # try once with API cookie if the plain request failed
            hp2, bp2 = post_form(PLAYER_URL, {"fid": fid, "time": ts, "sign": sign_p}, cookie_api or None)
            try:
                js2 = json.loads(bp2)
            except Exception:
                js2 = {}
            if hp2 == 200 and js2.get("msg") == "success":
                ok_players += 1
                data = js2.get("data", {})
                nick  = data.get("nickname")
                stove = data.get("stove_lv")
                prev  = rec.get("stove")
                rec.update({"nickname": nick, "stove": stove, "updated_at": int(time.time())})
                try:
                    if prev is not None and stove is not None and int(stove) > int(prev):
                        furnace_ups.append(f"🔥 `{fid}` {nick or ''} • {prev} ➜ {stove}")
                except Exception:
                    pass
        time.sleep(0.08)

    # ---- snapshot if no ups ----
    furnace_snapshot = []
    if not furnace_ups:
        for fid, rec in roster.items():
            if rec.get("stove") is not None:
                furnace_snapshot.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
        furnace_snapshot.sort()

    # ---- redeem for all roster fids ----
    ok_redeems = fail_redeems = 0
    redeem_lines = []

    PACING = float(os.environ.get("REDEEM_PACING_SECONDS", "6.0"))  # ← slow default to dodge 429

    def redeem_with_backoff(fid, code, variant):
        http, bodytxt = None, ""
        backoffs = [2, 4, 8, 16]  # exponential on hard limits
        for i in range(len(backoffs) + 1):
            http, bodytxt = redeem_once(cookie_api, fid, code, variant)
            if http in (429, 403):  # WAF / rate limit
                if i < len(backoffs):
                    time.sleep(backoffs[i])
                    continue
            break
        return http, bodytxt

    DEFAULT_ATTEMPTS = [
        # method, order, concat, ts, uppercase, lang, body, use_cookie
        ("POST","fixed","plain","s", False, False,"form", True),
        ("POST","sorted","plain","s", False, False,"form", True),
        ("POST","fixed","amp",  "s", False, False,"form", True),
        ("POST","fixed","key",  "s", False, False,"form", True),
        ("POST","fixed","prefix","s", False, False,"form", True),
        ("POST","fixed","plain","ms",False, False,"form", True),
        ("POST","fixed","plain","s", True,  False,"form", True),
        ("POST","fixed","plain","s", False, True, "form", True),
        ("POST","fixed","plain","s", False, False,"json", True),
        ("GET","fixed","plain","s", False, False,"form", True),
        ("GET","sorted","plain","s", False, False,"form", True),
        ("GET","fixed","amp",  "s", False, False,"form", True),
        ("GET","fixed","key",  "s", False, False,"form", True),
        ("GET","fixed","prefix","s", False, False,"form", True),
        ("GET","fixed","plain","ms",False, False,"form", True),
    ]

    def attempts_for_hint(h):
        if h == "GET":
            return [a for a in DEFAULT_ATTEMPTS if a[0]=="GET"] + [a for a in DEFAULT_ATTEMPTS if a[0]=="POST"]
        if h == "POST":
            return [a for a in DEFAULT_ATTEMPTS if a[0]=="POST"] + [a for a in DEFAULT_ATTEMPTS if a[0]=="GET"]
        return DEFAULT_ATTEMPTS

    method_hint = None

    for code in sorted(codes):
        safe_code = code[:3]+"…" if len(code)>3 else code
        for fid in sorted(roster.keys()):
            while True:
                ATTEMPTS = attempts_for_hint(method_hint)
                status = "UNKNOWN"
                flipped = False

                for (method, order, concat, ts_mode, upper, lang, body, use_cookie) in ATTEMPTS:
                    variant = {"method":method,"order":order,"concat":concat,"ts":ts_mode,
                               "uppercase":upper,"lang":lang,"body":body,"use_cookie":use_cookie}
                    http, bodytxt = redeem_with_backoff(fid, code, variant)
                    try:
                        rg = json.loads(bodytxt); msg = (rg.get("msg") or "").upper()
                    except Exception:
                        msg = (bodytxt or "")[:120].upper()

                    if DEBUG:
                        print(f"[DBG] {fid} {code} {method}/{order}/{concat}/{ts_mode} cookie=api "
                              f"HTTP={http} :: {msg or _short(bodytxt)}")

                    if http == 405 and method_hint is None:
                        method_hint = "GET" if method == "POST" else "POST"
                        flipped = True
                        break

                    if "SUCCESS" in msg:
                        status = "SUCCESS"; break
                    elif "RECEIVED" in msg:
                        status = "ALREADY"; break
                    elif "SAME TYPE EXCHANGE" in msg:
                        status = "SAME_TYPE"; break
                    elif "TIME ERROR" in msg:
                        status = "EXPIRED"; break
                    elif "CDK NOT FOUND" in msg:
                        status = "INVALID"; break
                    elif "PARAMS" in msg or "SIGN" in msg:
                        status = "PARAMS_ERROR" if "PARAMS" in msg else "SIGN_ERROR"
                        continue
                    else:
                        status = msg or f"HTTP_{http}"
                        if status not in ("UNKNOWN","PARSE_ERROR"):
                            break

                if flipped:
                    continue  # restart with new order

                ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
                ok_redeems += int(ok); fail_redeems += int(not ok)
                redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe_code} • {status}")
                time.sleep(PACING)  # global pacing between calls
                break

    # ---- save new checkpoint to Discord ----
    new_state = {
        "last_id_codes": str(last_codes),
        "last_id_ids":   str(last_ids),
        "roster":        roster,
        "ts":            int(time.time()),
    }
    msg = post_message_with_file(STATE_CH, "WOSBOT_STATE v1 (do not delete)",
                                 "wos_state.json", json.dumps(new_state, indent=2).encode("utf-8"))
    if msg and "id" in msg and prev_state_msg_id:
        delete_message(STATE_CH, prev_state_msg_id)

except Exception as e:
    had_unhandled_error = True
    error_summary = f"{type(e).__name__}: {e}"
    print("[FATAL] Unhandled exception:")
    traceback.print_exc()

# ========== SUMMARY ==========
parts = []
if 'added' in locals() and added:
    parts.append("**New IDs added**\n" + ", ".join(f"`{a}`" for a in added[:20]) + (" …" if len(added)>20 else ""))
if 'codes' in locals() and codes:
    parts.append("**Codes processed**\n" + ", ".join(f"`{c}`" for c in sorted(codes)))
if 'furnace_ups' in locals() and furnace_ups:
    parts.append("**Furnace level ups**\n" + "\n".join(furnace_ups[:15]) + (" \n…" if len(furnace_ups)>15 else ""))
elif 'furnace_snapshot' in locals() and furnace_snapshot:
    parts.append("**Furnace levels (snapshot)**\n" + "\n".join(furnace_snapshot[:15]) + (" \n…" if len(furnace_snapshot)>15 else ""))
if 'redeem_lines' in locals() and redeem_lines:
    parts.append("**Redeem results (first 25)**\n" + "\n".join(redeem_lines[:25]) + (" \n…" if len(redeem_lines)>25 else ""))

ok_players   = locals().get("ok_players", 0)
ok_redeems   = locals().get("ok_redeems", 0)
fail_redeems = locals().get("fail_redeems", 0)

summary = (
    f"**Daily Batch Summary**\n"
    f"Players checked: {ok_players}\n"
    f"Redeems: {ok_redeems} ok / {fail_redeems} failed\n"
)
if had_unhandled_error and error_summary:
    summary += f"\n\n⚠️ {error_summary}"

post_message(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))
sys.exit(0)
