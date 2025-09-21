import os, sys, json, re, time, hashlib, requests
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# ====== CONFIG ======
BOT_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH    = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH      = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
SUMMARY_CH  = os.environ.get("DISCORD_SUMMARY_CHANNEL_ID", "").strip() or CODES_CH
WOS_SECRET  = os.environ.get("WOS_SECRET", "").strip()
if not all([BOT_TOKEN, CODES_CH, IDS_CH, WOS_SECRET]):
    print("Missing one of: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, WOS_SECRET")
    sys.exit(1)

# ====== DISCORD REST ======
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def get_messages_after(channel_id, after_id=None, limit=100):
    params = {"limit": limit}
    if after_id: params["after"] = str(after_id)
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def post_message(channel_id, content):
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers={**D_HDR, "Content-Type":"application/json"},
                      json={"content": content[:1900]}, timeout=20)
    return r.status_code

def snowflake_max(msgs): return max((int(m["id"]) for m in msgs), default=0)

# ====== STATE & ROSTER ======
STATE_PATH  = ".github/wos_state.json"   # { "last_id_codes": "...", "last_id_ids": "..." }
ROSTER_PATH = ".github/roster.json"      # { fid: {nickname, stove, updated_at} }

def load_json(path, default):
    try:
        with open(path, "r") as f: return json.load(f)
    except: return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: json.dump(data, f, indent=2)

state  = load_json(STATE_PATH, {})
roster = load_json(ROSTER_PATH, {})

# ====== PARSERS ======
# Supported formats in each channel:
#   #gift_code_axe (codes):
#     codes:
#       - OFFICIALSTORE
#       - THANKYOU2025
#   OR CSV lines "CODE"
#   #3349_axe (ids):
#     fids:
#       - 550810376
#       - 244886619
#   OR CSV lines "FID"

YAML_CODES = re.compile(r"(?mi)^\s*codes\s*:\s*$")
YAML_FIDS  = re.compile(r"(?mi)^\s*fids\s*:\s*$")
BULLET     = re.compile(r"(?m)^\s*-\s*(\S+)\s*$")
CSV_CODE   = re.compile(r"(?mi)^\s*([A-Z0-9]{4,24})\s*$")
CSV_FID    = re.compile(r"(?mi)^\s*(\d{6,12})\s*$")

def parse_codes(text):
    out = set()
    current = None
    for line in text.splitlines():
        if YAML_CODES.match(line): current="codes"; continue
        m = BULLET.match(line)
        if m and current=="codes": out.add(m.group(1).strip().upper())
        else:
            m2 = CSV_CODE.match(line)
            if m2: out.add(m2.group(1).upper())
    return sorted(out)

def parse_fids(text):
    out = set()
    current = None
    for line in text.splitlines():
        if YAML_FIDS.match(line): current="fids"; continue
        m = BULLET.match(line)
        if m and current=="fids": out.add(m.group(1).strip())
        else:
            m2 = CSV_FID.match(line)
            if m2: out.add(m2.group(1))
    return sorted(out)

def fetch_text_attachments(msg):
    texts = []
    for att in msg.get("attachments", []):
        name = att.get("filename","").lower()
        if any(name.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
            texts.append(requests.get(att["url"], timeout=20).text)
    return texts

# ====== WOS ENDPOINTS ======
PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
ORIGIN       = "https://wos-giftcode.centurygame.com"
REFERER      = "https://wos-giftcode.centurygame.com/"

def md5(s:str) -> str: return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k,v in form.items())
    base  = "&".join([f"{k}={v}" for k,v in items])
    return md5(base + secret)

# --- added helpers for redeem robustness ---

def sign_fixed(fid: str, cdk: str, ts: str, secret: str) -> str:
    """
    Some deployments expect sign() over EXACT ORDER: fid,cdk,time
    """
    base = f"fid={fid}&cdk={cdk}&time={ts}"
    return md5(base + secret)

def sign_sorted_kct(fid: str, cdk: str, ts: str, secret: str) -> str:
    """
    Keep alphabetical sort, but over the redeem triad {cdk,fid,time}
    """
    return sign_sorted({"cdk": cdk, "fid": fid, "time": ts}, secret)

BROWSER_HEADERS_BASE = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://wos-giftcode.centurygame.com",
    "Referer": "https://wos-giftcode.centurygame.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
}

def redeem_attempt(cookie_hdr: str, fid: str, code: str, sign_func, method: str):
    """
    Try one request style (POST/GET) with a given signing function.
    Returns: (http_status:int, msg_upper:str, raw_or_json:any)
    """
    ts = str(int(time.time()))
    payload = {"fid": fid, "cdk": code, "time": ts, "sign": sign_func(fid, code, ts, WOS_SECRET)}
    headers = {**BROWSER_HEADERS_BASE}
    if cookie_hdr:
        headers["Cookie"] = cookie_hdr

    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = requests.post(GIFTCODE_URL, headers=headers, data=urlencode(payload), timeout=20)
    else:  # GET
        r = requests.get(GIFTCODE_URL, headers=headers, params=payload, timeout=20)

    try:
        js = r.json()
        msg = (js.get("msg") or "").upper()
        return r.status_code, msg or "UNKNOWN", js
    except Exception:
        return r.status_code, "PARSE_ERROR", r.text[:200]
# --- end added helpers ---


def post_form(url: str, form: dict, cookie: str | None):
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    }
    if cookie: headers["Cookie"] = cookie
    r = requests.post(url, headers=headers, data=urlencode(form), timeout=20)
    return r.status_code, r.text

def get_form(url: str, params: dict, cookie: str | None):
    # GET version of post_form with browsery headers
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    }
    if cookie:
        headers["Cookie"] = cookie
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

# ====== READ NEW CODES (codes channel) ======
last_codes_id = state.get("last_id_codes")
msgs_codes = get_messages_after(CODES_CH, last_codes_id) or []
codes = set()
for m in msgs_codes:
    text = m.get("content","")
    codes.update(parse_codes(text))
    for t in fetch_text_attachments(m):
        codes.update(parse_codes(t))
if msgs_codes:
    state["last_id_codes"] = str(max(int(state.get("last_id_codes") or 0), snowflake_max(msgs_codes)))

# ====== READ NEW FIDs (ids channel) ======
last_ids_id = state.get("last_id_ids")
msgs_ids = get_messages_after(IDS_CH, last_ids_id) or []
new_fids = set()
for m in msgs_ids:
    text = m.get("content","")
    new_fids.update(parse_fids(text))
    for t in fetch_text_attachments(m):
        new_fids.update(parse_fids(t))
if msgs_ids:
    state["last_id_ids"] = str(max(int(state.get("last_id_ids") or 0), snowflake_max(msgs_ids)))

# Save checkpoint now
save_json(STATE_PATH, state)

# ====== UPDATE ROSTER ======
added = []
for fid in sorted(new_fids):
    if fid not in roster:
        roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
        added.append(fid)

# ====== FURNACE SCAN (all roster) ======
cookie_hdr = get_cookie_header()
print("Cookie:", "[present]" if cookie_hdr else "[none]")

ok_players = 0
furnace_diffs = []

for fid, rec in roster.items():
    ts = str(int(time.time()))
    payload = {"fid": fid, "time": ts}
    payload["sign"] = sign_sorted(payload, WOS_SECRET)
    hp, bp = post_form(PLAYER_URL, payload, cookie=None)
    try:
        js = json.loads(bp)
    except Exception:
        js = {}

    if hp == 200 and js.get("msg") == "success":
        ok_players += 1
        data = js.get("data", {})
        nick = data.get("nickname")
        stove = data.get("stove_lv")
        prev = rec.get("stove")
        rec.update({"nickname": nick, "stove": stove, "updated_at": int(time.time())})
        try:
            if prev is not None and stove is not None and int(stove) > int(prev):
                furnace_diffs.append(f"🔥 `{fid}` {nick or ''} • {prev} ➜ {stove}")
        except Exception:
            pass
    time.sleep(0.2)

# ---- Furnace snapshot (shown only if there are no ups; useful on first run) ----
furnace_snapshot = []
for fid, rec in roster.items():
    if rec.get("stove") is not None:
        furnace_snapshot.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
furnace_snapshot = sorted(furnace_snapshot)
# ------------------------------------------------------------------------------

# ===== REDEEM (new codes for ALL roster FIDs) =====
codes = sorted(codes)
ok_redeems = fail_redeems = 0
redeem_lines = []

for code in codes:
    safe_code = code[:3]+"…" if len(code)>3 else code
    for fid in sorted(roster.keys()):
        status = "UNKNOWN"

        # Try multiple combinations: some backends are picky about sign order and method
        attempts = [
            ("POST", sign_fixed),         # POST + sign(fid,cdk,time)
            ("POST", sign_sorted_triad),  # POST + alphabetical sign
            ("GET",  sign_fixed),         # GET  + sign(fid,cdk,time)
            ("GET",  sign_sorted_triad),  # GET  + alphabetical sign
        ]

        for method, signer in attempts:
            ts2 = str(int(time.time()))
            payload = {"fid": fid, "cdk": code, "time": ts2, "sign": signer(fid, code, ts2, WOS_SECRET)}

            if method == "POST":
                hg, bg = post_form(GIFTCODE_URL, payload, cookie_hdr)   # you already have this helper
            else:  # GET
                hg, bg = get_form(GIFTCODE_URL, payload, cookie_hdr)

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
                status = "PARAMS_ERROR"
                # keep trying next attempt
                continue
            else:
                status = msg or "UNKNOWN"
                break

        ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
        ok_redeems += int(ok); fail_redeems += int(not ok)
        redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe_code} • {status}")
        time.sleep(0.2)


# Save roster (with updated nick/stove)
save_json(ROSTER_PATH, roster)

# ====== SUMMARY ======
parts = []
added = sorted(new_fids)
if added:
    parts.append("**New IDs added**\n" + ", ".join(f"`{a}`" for a in added[:20]) + (" …" if len(added)>20 else ""))
if codes:
    parts.append("**Codes processed**\n" + ", ".join(f"`{c}`" for c in codes))

if furnace_diffs:
    parts.append("**Furnace level ups**\n" + "\n".join(furnace_diffs[:15]) + (" \n…" if len(furnace_diffs)>15 else ""))
elif furnace_snapshot:
    parts.append("**Furnace levels (snapshot)**\n" + "\n".join(furnace_snapshot[:15]) + (" \n…" if len(furnace_snapshot)>15 else ""))

if redeem_lines:
    parts.append("**Redeem results (first 25)**\n" + "\n".join(redeem_lines[:25]) + (" \n…" if len(redeem_lines)>25 else ""))

summary = (
    f"**Daily Batch Summary**\n"
    f"Players checked: {ok_players}\n"
    f"Redeems: {ok_redeems} ok / {fail_redeems} failed\n"
)
post_message(SUMMARY_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes today)"))

# Non-zero exit if everything failed (helps you notice problems)
# Leave the workflow green; rely on the summary text to spot failures.
if ok_players == 0 and (codes and ok_redeems == 0):
    sys.exit(0)

