import os, sys, json, re, time, io, base64, hashlib, traceback, random
import requests
from urllib.parse import urlencode
from PIL import Image, ImageOps, ImageFilter
try:
    import pytesseract
except Exception:
    pytesseract = None

from playwright.sync_api import sync_playwright

# ========== CONFIG ==========
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()

CAPTCHA_PROVIDER     = (os.environ.get("CAPTCHA_PROVIDER", "local") or "local").lower()   # 'local' or '2captcha'
TWOCAPTCHA_API_KEY   = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()
REDEEM_PACING        = float(os.environ.get("REDEEM_PACING_SECONDS", "1.5"))
DEBUG                = os.environ.get("DEBUG", "0") == "1"

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing one of required envs.")
    sys.exit(0)

# ========== DISCORD ==========
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def post_message(channel_id, content):
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers={**D_HDR,"Content-Type":"application/json"},
                      json={"content": content[:1900]}, timeout=20)
    if not r.ok: print("[ERR] Discord POST", r.status_code, r.text[:200])
    return r.status_code

def get_messages_after(channel_id, after_id=None, limit=100):
    params = {"limit": limit}
    if after_id: params["after"] = str(after_id)
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=20)
    if not r.ok: return []
    try: return r.json()
    except: return []

def latest_messages(channel_id, limit=25):
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params={"limit": limit}, timeout=20)
    if not r.ok: return []
    try: return r.json()
    except: return []

def post_message_with_file(channel_id, content, filename, bytes_data):
    payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
    files = {'files[0]': (filename, bytes_data, 'application/json')}
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers=D_HDR, data={"payload_json": json.dumps(payload)}, files=files, timeout=30)
    return r.json() if r.ok else {}

def delete_message(channel_id, message_id):
    try: requests.delete(f"{D_API}/channels/{channel_id}/messages/{message_id}", headers=D_HDR, timeout=15)
    except: pass

def get_me():
    r = requests.get(f"{D_API}/users/@me", headers=D_HDR, timeout=15); r.raise_for_status()
    return r.json()["id"]

def find_latest_state_message(channel_id, bot_user_id):
    for m in latest_messages(channel_id, 25):
        if str(m.get("author",{}).get("id")) != str(bot_user_id): continue
        for att in m.get("attachments", []):
            if att.get("filename") == "wos_state.json":
                try:
                    txt = requests.get(att["url"], timeout=20).text
                    return m["id"], json.loads(txt)
                except:
                    return m["id"], {}
    return None, {}

# ========== PARSERS ==========
YAML_CODES = re.compile(r"(?mi)^\s*codes\s*:\s*$")
YAML_FIDS  = re.compile(r"(?mi)^\s*fids\s*:\s*$")
BULLET     = re.compile(r"(?m)^\s*-\s*(\S+)\s*$")
CSV_CODE   = re.compile(r"(?mi)^\s*([A-Za-z0-9]{4,24})\s*$")
CSV_FID    = re.compile(r"(?mi)^\s*(\d{6,12})\s*$")

def parse_codes(text):
    out, cur = set(), None
    for line in text.splitlines():
        if YAML_CODES.match(line): cur="codes"; continue
        m = BULLET.match(line)
        if m and cur=="codes": out.add(m.group(1).strip().upper()); continue
        m2 = CSV_CODE.match(line)
        if m2: out.add(m2.group(1).upper())
    return out

def parse_fids(text):
    out, cur = set(), None
    for line in text.splitlines():
        if YAML_FIDS.match(line): cur="fids"; continue
        m = BULLET.match(line)
        if m and cur=="fids": out.add(m.group(1).strip()); continue
        m2 = CSV_FID.match(line)
        if m2: out.add(m2.group(1))
    return out

# ========== WOS ==========
PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
ORIGIN       = "https://wos-giftcode.centurygame.com"
REFERER      = "https://wos-giftcode.centurygame.com/"

BROWSER_HEADERS_BASE = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": ORIGIN,
    "Referer": REFERER,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
}

def md5(s:str) -> str: return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_with_captcha(fid:str, cdk:str, captcha:str, ts:str) -> str:
    # Exact to the cURL you shared: fid, cdk, captcha_code, time (ms) + secret
    base = f"fid={fid}&cdk={cdk}&captcha_code={captcha}&time={ts}"
    return md5(base + WOS_SECRET)

def sign_sorted(form: dict) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + WOS_SECRET)

def post_form(url: str, form: dict, cookie: str | None):
    headers = {**BROWSER_HEADERS_BASE, "Content-Type": "application/x-www-form-urlencoded"}
    if cookie: headers["Cookie"] = cookie
    r = requests.post(url, headers=headers, data=urlencode(form), timeout=20)
    return r.status_code, r.text

def get_cookie_header_and_captcha_png():
    """
    Opens the gift page, grabs cookies and tries to screenshot the captcha element.
    Returns: (cookie_header:str, captcha_png_bytes or None)
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(REFERER, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)

        png = None
        # Try a few likely selectors:
        selectors = [
            'img[src*="captcha"]',
            'img[alt*="captcha" i]',
            'canvas',
            '[class*="captcha"] img',
        ]
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    png = el.screenshot(type="png")
                    break
            except: pass

        cookies = ctx.cookies()
        browser.close()

    pairs = []
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if domain.endswith("wos-giftcode.centurygame.com"):
            pairs.append(f"{c['name']}={c['value']}")
    return ("; ".join(pairs), png)

# ---- OCR (local) ----
def ocr_from_png(png_bytes: bytes) -> str:
    if not png_bytes or not pytesseract:
        return ""
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    # Simple threshold
    img = img.point(lambda x: 0 if x < 150 else 255, mode='1')
    txt = pytesseract.image_to_string(
        img, config='--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    )
    return re.sub(r'[^A-Za-z0-9]', '', txt or '').strip()[:8]

# ---- 2Captcha ----
def solve_2captcha(png_bytes: bytes) -> str:
    if not TWOCAPTCHA_API_KEY or not png_bytes:
        return ""
    b64 = base64.b64encode(png_bytes).decode()
    # submit
    s = requests.post("http://2captcha.com/in.php", data={
        "key": TWOCAPTCHA_API_KEY,
        "method": "base64",
        "body": b64,
        "regsense": 1,
        "min_len": 4,
        "max_len": 8,
        "json": 1
    }, timeout=30).json()
    if s.get("status") != 1:
        return ""
    rid = s.get("request")
    # poll
    for _ in range(15):
        time.sleep(3)
        g = requests.get("http://2captcha.com/res.php",
                         params={"key": TWOCAPTCHA_API_KEY, "action":"get", "id": rid, "json":1}, timeout=20).json()
        if g.get("status") == 1:
            return re.sub(r'[^A-Za-z0-9]', '', g.get("request",""))[:8]
        if g.get("request") not in ("CAPCHA_NOT_READY", None):
            break
    return ""

# ========== MAIN ==========
started = time.time()
post_message(STATE_CH, "🟢 WOS daily run starting…")

had_unhandled_error = False
error_summary = ""

try:
    bot_id = get_me()
    prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
    last_codes = int(state.get("last_id_codes") or 0)
    last_ids   = int(state.get("last_id_ids") or 0)
    roster     = state.get("roster") or {}

    # read codes
    msgs_codes = get_messages_after(CODES_CH, last_codes)
    codes = set()
    for m in msgs_codes:
        txt = (m.get("content") or "")
        codes |= parse_codes(txt)
        for att in m.get("attachments", []):
            n = att.get("filename","").lower()
            if any(n.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
                try:
                    r = requests.get(att["url"], timeout=20)
                    if r.ok: codes |= parse_codes(r.text)
                except: pass
        last_codes = max(last_codes, int(m.get("id", last_codes)))

    # read fids
    msgs_ids = get_messages_after(IDS_CH, last_ids)
    new_fids = set()
    for m in msgs_ids:
        txt = (m.get("content") or "")
        new_fids |= parse_fids(txt)
        for att in m.get("attachments", []):
            n = att.get("filename","").lower()
            if any(n.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
                try:
                    r = requests.get(att["url"], timeout=20)
                    if r.ok: new_fids |= parse_fids(r.text)
                except: pass
        last_ids = max(last_ids, int(m.get("id", last_ids)))

    # update roster
    added = []
    for fid in sorted(new_fids):
        if fid not in roster:
            roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
            added.append(fid)

    # browser cookie + captcha image
    cookie_hdr, captcha_png = get_cookie_header_and_captcha_png()

    # solve captcha
    CAPTCHA = ""
    if CAPTCHA_PROVIDER == "local":
        CAPTCHA = ocr_from_png(captcha_png)
        # retry a couple times if short
        if len(CAPTCHA) < 4:
            time.sleep(1.0)
            CAPTCHA = ocr_from_png(captcha_png)
    elif CAPTCHA_PROVIDER == "2captcha":
        CAPTCHA = solve_2captcha(captcha_png)

    if DEBUG:
        post_message(STATE_CH, f"Solver={CAPTCHA_PROVIDER}; captcha={'OK' if CAPTCHA else 'FAIL'}")

    # furnace scan
    ok_players = 0
    furnace_ups, furnace_snap = [], []
    for fid, rec in roster.items():
        ts = str(int(time.time()))
        payload = {"fid": fid, "time": ts}
        payload["sign"] = sign_sorted(payload)
        hp, bp = post_form(PLAYER_URL, payload, cookie=None)
        try: js = json.loads(bp)
        except: js = {}
        if hp == 200 and js.get("msg") == "success":
            ok_players += 1
            d = js.get("data", {})
            nick, stove = d.get("nickname"), d.get("stove_lv")
            prev = rec.get("stove")
            rec.update({"nickname": nick, "stove": stove, "updated_at": int(time.time())})
            try:
                if prev is not None and stove is not None and int(stove) > int(prev):
                    furnace_ups.append(f"🔥 `{fid}` {nick or ''} • {prev} ➜ {stove}")
            except: pass
        time.sleep(0.1)

    if not furnace_ups:
        for fid, rec in roster.items():
            if rec.get("stove") is not None:
                furnace_snap.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
        furnace_snap.sort()

    # redeem (requires captcha)
    ok_redeems = fail_redeems = 0
    redeem_lines = []
    for code in sorted(codes):
        safe = code[:3]+"…" if len(code)>3 else code
        for fid in sorted(roster.keys()):
            if not CAPTCHA:
                status = "NO_CAPTCHA"
            else:
                ts_ms = str(int(time.time() * 1000))
                form  = {
                    "fid": fid,
                    "cdk": code,
                    "captcha_code": CAPTCHA,
                    "time": ts_ms,
                    "sign": sign_with_captcha(fid, code, CAPTCHA, ts_ms),
                }
                http, body = post_form(GIFTCODE_URL, form, cookie_hdr or None)
                try:
                    rg = json.loads(body); msg = (rg.get("msg") or "").upper()
                except:
                    msg = (body or "")[:80].upper()

                if "SUCCESS" in msg:
                    status = "SUCCESS"
                elif "RECEIVED" in msg:
                    status = "ALREADY"
                elif "SAME TYPE EXCHANGE" in msg:
                    status = "SAME_TYPE"
                elif "TIME ERROR" in msg:
                    status = "EXPIRED"
                elif "CDK NOT FOUND" in msg:
                    status = "INVALID"
                elif "TOO MANY" in msg or http == 429:
                    status = "RATE_LIMIT"
                elif "PARAMS" in msg or "SIGN" in msg:
                    status = "PARAMS_ERROR" if "PARAMS" in msg else "SIGN_ERROR"
                elif http == 405:
                    status = "HTTP_405"
                else:
                    status = msg or f"HTTP_{http}"

            ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
            ok_redeems += int(ok); fail_redeems += int(not ok)
            redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe} • {status}")

            # pacing with small jitter
            time.sleep(REDEEM_PACING + random.uniform(0, 0.4))

    # save state back to Discord
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
    traceback.print_exc()

# summary
parts = []
if 'added' in locals() and added:
    parts.append("**New IDs added**\n" + ", ".join(f"`{a}`" for a in added[:20]) + (" …" if len(added)>20 else ""))
if 'codes' in locals() and codes:
    parts.append("**Codes processed**\n" + ", ".join(f"`{c}`" for c in sorted(codes)))
if 'furnace_ups' in locals() and furnace_ups:
    parts.append("**Furnace level ups**\n" + "\n".join(furnace_ups[:15]) + (" \n…" if len(furnace_ups)>15 else ""))
elif 'furnace_snap' in locals() and furnace_snap:
    parts.append("**Furnace levels (snapshot)**\n" + "\n".join(furnace_snap[:15]) + (" \n…" if len(furnace_snap)>15 else ""))
if 'redeem_lines' in locals() and redeem_lines:
    parts.append("**Redeem results (first 25)**\n" + "\n".join(redeem_lines[:25]) + (" \n…" if len(redeem_lines)>25 else ""))

summary = (
    f"**Daily Batch Summary**\n"
    f"Players checked: {locals().get('ok_players',0)}\n"
    f"Redeems: {locals().get('ok_redeems',0)} ok / {locals().get('fail_redeems',0)} failed\n"
    f"Captcha solver: {CAPTCHA_PROVIDER} ({'OK' if locals().get('CAPTCHA') else 'FAIL'})\n"
)
if had_unhandled_error and error_summary:
    summary += f"\n⚠️ {error_summary}"

post_message(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))
sys.exit(0)
