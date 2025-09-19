import os, sys, json, re, time, hashlib, requests
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# ====== CONFIG FROM SECRETS ======
BOT_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID  = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
WOS_SECRET  = os.environ.get("WOS_SECRET", "").strip()
if not BOT_TOKEN or not CHANNEL_ID or not WOS_SECRET:
    print("Missing DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID / WOS_SECRET"); sys.exit(1)

# ====== DISCORD REST (no gateway) ======
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

# ====== STATE & ROSTER PERSISTENCE ======
STATE_PATH  = ".github/wos_state.json"   # stores last processed msg ID
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

# ====== PARSERS (how you post in Discord) ======
# Supported message formats (content or .txt/.csv/.yaml attachment):
# 1) YAML-ish lists:
#    codes:
#      - OFFICIALSTORE
#      - THANKYOU2025
#    fids:
#      - 550810376
#      - 244886619
# 2) CSV:
#    FID,CODE
#    550810376, OFFICIALSTORE
#    244886619, THANKYOU2025

YAML_CODES = re.compile(r"(?mi)^\s*codes\s*:\s*$")
YAML_FIDS  = re.compile(r"(?mi)^\s*fids\s*:\s*$")
BULLET     = re.compile(r"(?m)^\s*-\s*(\S+)\s*$")
CSV_LINE   = re.compile(r"(?mi)^\s*(\d{6,12})\s*,\s*([A-Z0-9]{4,24})\s*$")

def parse_payload(text):
    codes, fids = set(), set()
    # YAML-ish
    if YAML_CODES.search(text) and YAML_FIDS.search(text):
        current = None
        for line in text.splitlines():
            if YAML_CODES.match(line): current="codes"; continue
            if YAML_FIDS.match(line):  current="fids";  continue
            m = BULLET.match(line)
            if m and current=="codes": codes.add(m.group(1).strip().upper())
            elif m and current=="fids": fids.add(m.group(1).strip())
    # CSV
    for m in CSV_LINE.finditer(text):
        fids.add(m.group(1))
        codes.add(m.group(2).upper())
    return sorted(codes), sorted(fids)

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

# ====== READ NEW DISCORD INSTRUCTIONS ======
after_id = state.get("last_id")
msgs = get_messages_after(CHANNEL_ID, after_id)
if not msgs and not after_id:
    # first run: read last page anyway
    msgs = get_messages_after(CHANNEL_ID, None)

all_codes, new_fids = set(), set()
if msgs:
    for m in msgs:
        txt = m.get("content","")
        c, f = parse_payload(txt)
        for t in fetch_text_attachments(m):
            c2, f2 = parse_payload(t)
            c += c2; f += f2
        all_codes.update(c); new_fids.update(f)

# Update checkpoint now so we don't reprocess
if msgs:
    state["last_id"] = str(max(int(state.get("last_id") or 0), snowflake_max(msgs)))
    save_json(STATE_PATH, state)

# ====== UPDATE ROSTER ======
added = []
for fid in sorted(new_fids):
    if fid not in roster:
        roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
        added.append(fid)

# ====== SCAN ALL ROSTER FOR FURNACE UPDATES ======
cookie_hdr = get_cookie_header()
print("Cookie:", "[present]" if cookie_hdr else "[none]")

furnace_diffs = []
ok_players = 0

for fid, rec in roster.items():
    ts = str(int(time.time()))
    form_p = {"fid": fid, "time": ts, "sign": sign_sorted({"fid": fid, "time": ts}, WOS_SECRET)}
    hp, bp = post_form(PLAYER_URL, form_p, cookie=None)
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
        if prev is not None and stove is not None and int(stove) > int(prev):
            furnace_diffs.append(f"🔥 `{fid}` {nick or ''} • {prev} ➜ {stove}")
    else:
        # Keep going; we’ll report in summary
        pass
    time.sleep(0.2)

# ====== REDEEM NEW CODES (for all roster FIDs) ======
safe_codes = [c[:3]+"…" if len(c)>3 else c for c in sorted(all_codes)]
ok_redeems = fail_redeems = 0
redeem_lines = []

for code in sorted(all_codes):
    for fid in sorted(roster.keys()):
        ts2 = str(int(time.time()))
        form_g = {"fid": fid, "cdk": code, "time": ts2,
                  "sign": sign_sorted({"fid": fid, "cdk": code, "time": ts2}, WOS_SECRET)}
        hg, bg = post_form(GIFTCODE_URL, form_g, cookie_hdr)

        status = "UNKNOWN"
        try:
            rg = json.loads(bg); msg = (rg.get("msg") or "").upper()
            if "SUCCESS" in msg: status = "SUCCESS"
            elif "RECEIVED" in msg: status = "ALREADY"
            elif "SAME TYPE EXCHANGE" in msg: status = "SAME_TYPE"
            elif "TIME ERROR" in msg: status = "EXPIRED"
            elif "CDK NOT FOUND" in msg: status = "INVALID"
            elif "PARAMS" in msg: status = "PARAMS_ERROR"
            else: status = msg or "UNKNOWN"
        except Exception:
            status = "PARSE_ERROR"

        ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
        if ok: ok_redeems += 1
        else:  fail_redeems += 1
        emoji = "✅" if ok else "❌"
        redeem_lines.append(f"{emoji} {fid} • {code} • {status}")
        time.sleep(0.2)

# ====== SAVE ROSTER ======
save_json(ROSTER_PATH, roster)

# ====== POST SUMMARY ======
parts = []
if added:
    parts.append("**New IDs added to roster**\n" + ", ".join(f"`{a}`" for a in added[:20]) + ("" if len(added)<=20 else " …"))
if all_codes:
    parts.append("**Codes processed today**\n" + ", ".join(f"`{c}`" for c in sorted(all_codes)))
if furnace_diffs:
    parts.append("**Furnace level ups**\n" + "\n".join(furnace_diffs[:15]) + ("" if len(furnace_diffs)<=15 else "\n…"))
if redeem_lines:
    parts.append("**Redeem results (first 25)**\n" + "\n".join(redeem_lines[:25]) + ("" if len(redeem_lines)<=25 else "\n…"))

summary = (
    f"**Daily Batch Summary**\n"
    f"Players checked: {ok_players}\n"
    f"Redeems: {ok_redeems} ok / {fail_redeems} failed\n"
)
post_message(CHANNEL_ID, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes today)"))

# Exit non-zero if everything failed (useful for alerts)
if ok_players == 0 or (all_codes and ok_redeems == 0):
    sys.exit(1)
