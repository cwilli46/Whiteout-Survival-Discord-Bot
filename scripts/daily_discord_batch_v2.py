import os, sys, json, re, time, hashlib, requests, traceback
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# ========== CONFIG ==========
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()  # also used for summaries
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()
DEBUG      = os.environ.get("DEBUG","0") == "1"

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing one of: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, DISCORD_STATE_CHANNEL_ID, WOS_SECRET")
    sys.exit(0)  # soft exit so workflow stays green for easier iteration

# ========== DISCORD REST ==========
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def post_message(channel_id, content):
    r = requests.post(
        f"{D_API}/channels/{channel_id}/messages",
        headers={**D_HDR, "Content-Type":"application/json"},
        json={"content": content[:1900]},
        timeout=20,
    )
    if not r.ok:
        print(f"[ERR] Discord POST ch={channel_id} status={r.status_code} body={r.text[:200]}")
    return r.status_code

def get_messages_after(channel_id, after_id=None, limit=100):
    params = {"limit": limit}
    if after_id: params["after"] = str(after_id)
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=20)
    if not r.ok:
        print(f"[ERR] Discord GET messages ch={channel_id} status={r.status_code} body={r.text[:200]}")
        return []
    try:
        return r.json()
    except Exception:
        print(f"[ERR] Discord GET messages JSON parse failed ch={channel_id}")
        return []

def post_message_with_file(channel_id, content, filename, bytes_data):
    payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
    files = {'files[0]': (filename, bytes_data, 'application/json')}
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers=D_HDR,
                      data={"payload_json": json.dumps(payload)},
                      files=files, timeout=30)
    if not r.ok:
        print(f"[ERR] Discord POST file ch={channel_id} status={r.status_code} body={r.text[:200]}")
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
                resp = requests.get(att["url"], timeout=20)
                if resp.ok: texts.append(resp.text)
                else: print(f"[WARN] Attachment fetch failed {name} status={resp.status_code}")
            except Exception:
                print(f"[WARN] Attachment fetch threw for {name}")
    return texts

# ========== WOS ENDPOINTS ==========
PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
ORIGIN       = "https://wos-giftcode.centurygame.com"
REFERER      = "https://wos-giftcode.centurygame.com/"

def md5(s:str) -> str: return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + secret)

def sign_fixed(fid: str, cdk: str, ts: str, secret: str) -> str:
    base = f"fid={fid}&cdk={cdk}&time={ts}"
    return md5(base + secret)

def sign_sorted_triad(fid: str, cdk: str, ts: str, secret: str) -> str:
    return sign_sorted({"cdk": cdk, "fid": fid, "time": ts}, secret)

BROWSER_HEADERS_BASE = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": ORIGIN,
    "Referer": REFERER,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
}

def post_form(url: str, form: dict, cookie: str | None):
    headers = {
        **BROWSER_HEADERS_BASE,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if cookie: headers["Cookie"] = cookie
    r = requests.post(url, headers=headers, data=urlencode(form), timeout=20)
    return r.status_code, r.text

def get_form(url: str, params: dict, cookie: str | None):
    headers = {**BROWSER_HEADERS_BASE}
    if cookie: headers["Cookie"] = cookie
    r = requests.get(url, headers=headers, params=params, timeout=20)
    return r.status_code, r.text

def get_cookie_header():
    print("Acquiring site cookie via headless Chromium…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(REFERER, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(3000)
        cookies = ctx.cookies()
        browser.close()
    pairs = []
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if domain.endswith("wos-giftcode.centurygame.com"):
            pairs.append(f"{c['name']}={c['value']}")
    return "; ".join(pairs)

def redeem_attempt(cookie_hdr: str, fid: str, code: str, sign_func, method: str):
    ts = str(int(time.time()))
    payload = {"fid": fid, "cdk": code, "time": ts, "sign": sign_func(fid, code, ts, WOS_SECRET)}
    if method == "POST":
        return post_form(GIFTCODE_URL, payload, cookie_hdr)
    else:
        return get_form(GIFTCODE_URL, payload, cookie_hdr)

# ========== MAIN ==========
had_unhandled_error = False
error_summary = ""
startup_rc = post_message(STATE_CH, "🟢 WOS daily run starting…")
print(f"[INFO] Startup ping HTTP={startup_rc}")

try:
    bot_id = get_me()

    # Load last state (from Discord attachment), else fresh
    prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
    last_codes = int(state.get("last_id_codes") or 0)
    last_ids   = int(state.get("last_id_ids") or 0)
    roster     = state.get("roster") or {}   # {fid: {nickname, stove, updated_at}}

    # Read codes since last checkpoint
    msgs_codes = get_messages_after(CODES_CH, last_codes)
    codes = set()
    for m in msgs_codes:
        txt = m.get("content","") or ""
        codes |= parse_codes(txt)
        for t in fetch_text_attachments(m):
            codes |= parse_codes(t)
        last_codes = max(last_codes, int(m["id"]))

    # Read FIDs since last checkpoint
    msgs_ids = get_messages_after(IDS_CH, last_ids)
    new_fids = set()
    for m in msgs_ids:
        txt = m.get("content","") or ""
        new_fids |= parse_fids(txt)
        for t in fetch_text_attachments(m):
            new_fids |= parse_fids(t)
        last_ids = max(last_ids, int(m["id"]))

    # Update roster
    added = []
    for fid in sorted(new_fids):
        if fid not in roster:
            roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
            added.append(fid)

    # Furnace scan
    cookie_hdr = get_cookie_header()
    print("Cookie:", "[present]" if cookie_hdr else "[none]")

    ok_players = 0
    furnace_ups = []
    for fid, rec in roster.items():
        ts = str(int(time.time()))
        payload = {"fid": fid, "time": ts, "sign": sign_sorted({"fid": fid, "time": ts}, WOS_SECRET)}
        hp, bp = post_form(PLAYER_URL, payload, cookie=None)
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
        time.sleep(0.15)

    # Snapshot if no ups (first run)
    furnace_snapshot = []
    if not furnace_ups:
        for fid, rec in roster.items():
            if rec.get("stove") is not None:
                furnace_snapshot.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
        furnace_snapshot.sort()

    # Redeem for all roster FIDs
    ok_redeems = fail_redeems = 0
    redeem_lines = []
    for code in sorted(codes):
        safe_code = code[:3]+"…" if len(code)>3 else code
        for fid in sorted(roster.keys()):
            status = "UNKNOWN"
            attempts = [
                ("POST", sign_fixed),
                ("POST", sign_sorted_triad),
                ("GET",  sign_fixed),
                ("GET",  sign_sorted_triad),
            ]
            for method, signer in attempts:
                hg, bg = redeem_attempt(cookie_hdr, fid, code, signer, method)
                try:
                    rg = json.loads(bg); msg = (rg.get("msg") or "").upper()
                except Exception:
                    msg = "PARSE_ERROR"

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
                elif "PARAMS" in msg:
                    status = "PARAMS_ERROR"; continue
                else:
                    status = msg or "UNKNOWN"; break

            ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
            ok_redeems += int(ok); fail_redeems += int(not ok)
            redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe_code} • {status}")
            time.sleep(0.15)

    # Save next checkpoint/state back to Discord (attachment)
    new_state = {
        "last_id_codes": str(last_codes),
        "last_id_ids":   str(last_ids),
        "roster":        roster,
        "ts":            int(time.time()),
    }
    state_bytes = json.dumps(new_state, indent=2).encode("utf-8")
    msg = post_message_with_file(STATE_CH, "WOSBOT_STATE v1 (do not delete)", "wos_state.json", state_bytes)
    if msg and "id" in msg and prev_state_msg_id:
        delete_message(STATE_CH, prev_state_msg_id)

except Exception as e:
    had_unhandled_error = True
    error_summary = f"{type(e).__name__}: {e}"
    print("[FATAL] Unhandled exception. Traceback follows:")
    traceback.print_exc()

# Summary post (always try)
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

ok_players = locals().get("ok_players", 0)
ok_redeems = locals().get("ok_redeems", 0)
fail_redeems = locals().get("fail_redeems", 0)

summary = (
    f"**Daily Batch Summary**\n"
    f"Players checked: {ok_players}\n"
    f"Redeems: {ok_redeems} ok / {fail_redeems} failed\n"
)
if had_unhandled_error and error_summary:
    summary += f"\n\n⚠️ {error_summary}"

rc = post_message(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))
print(f"[INFO] Posted summary HTTP={rc}")

# Keep workflow green while you iterate; you can still spot issues in the summary/logs
sys.exit(0)
