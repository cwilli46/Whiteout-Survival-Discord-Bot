import os, sys, json, re, time, io, base64, hashlib, random, traceback
import requests
from urllib.parse import urlencode

# --- optional OCR libs (free) ---
try:
    from PIL import Image, ImageOps, ImageFilter
except Exception:
    Image = ImageOps = ImageFilter = None

try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    import easyocr
except Exception:
    easyocr = None

from playwright.sync_api import sync_playwright

# ================= CONFIG =================
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()   # also used for summaries
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()
REDEEM_PACING = float(os.environ.get("REDEEM_PACING_SECONDS", "1.8"))
DEBUG = os.environ.get("DEBUG","0") == "1"

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing required envs: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, DISCORD_STATE_CHANNEL_ID, WOS_SECRET")
    sys.exit(0)  # keep workflow green during setup

# ================= DISCORD REST =================
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def post_message(channel_id, content):
    try:
        r = requests.post(
            f"{D_API}/channels/{channel_id}/messages",
            headers={**D_HDR,"Content-Type":"application/json"},
            json={"content": content[:1900]},
            timeout=25
        )
        if not r.ok:
            print("[ERR] Discord POST", r.status_code, r.text[:200])
        return r.status_code
    except Exception as e:
        print("[ERR] Discord POST threw:", e)
        return 0

def _get_messages(channel_id, params):
    try:
        r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=25)
        if not r.ok:
            print(f"[ERR] Discord GET messages ch={channel_id} status={r.status_code} body={r.text[:200]}")
            return []
        return r.json()
    except Exception:
        return []

def get_messages_after(channel_id, after_id=None, limit=100):
    p = {"limit": limit}
    if after_id: p["after"] = str(after_id)
    return _get_messages(channel_id, p)

def latest_messages(channel_id, limit=25):
    return _get_messages(channel_id, {"limit": limit})

def post_message_with_file(channel_id, content, filename, bytes_data):
    try:
        payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
        files = {'files[0]': (filename, bytes_data, 'application/json')}
        r = requests.post(
            f"{D_API}/channels/{channel_id}/messages",
            headers=D_HDR,
            data={"payload_json": json.dumps(payload)},
            files=files, timeout=40
        )
        if not r.ok:
            print(f"[ERR] Discord POST file ch={channel_id} status={r.status_code} body={r.text[:200]}")
        return r.json() if r.ok else {}
    except Exception as e:
        print("[ERR] Discord POST file threw:", e)
        return {}

def delete_message(channel_id, message_id):
    try:
        requests.delete(f"{D_API}/channels/{channel_id}/messages/{message_id}", headers=D_HDR, timeout=15)
    except Exception:
        pass

def get_me():
    r = requests.get(f"{D_API}/users/@me", headers=D_HDR, timeout=15)
    r.raise_for_status()
    return r.json()["id"]

def find_latest_state_message(channel_id, bot_user_id):
    for m in latest_messages(channel_id, 25):
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

# ================= PARSERS =================
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

# ================= WOS API =================
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

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join(f"{k}={v}" for k, v in items)
    return md5(base + secret)

def sign_with_captcha(fid:str, cdk:str, captcha:str, ts_ms:str) -> str:
    # server expects: fid & cdk & captcha_code & time (ms), then + secret
    base = f"fid={fid}&cdk={cdk}&captcha_code={captcha}&time={ts_ms}"
    return md5(base + WOS_SECRET)

def post_form(url: str, form: dict, cookie: str | None):
    headers = {**BROWSER_HEADERS_BASE, "Content-Type": "application/x-www-form-urlencoded"}
    if cookie: headers["Cookie"] = cookie
    r = requests.post(url, headers=headers, data=urlencode(form), timeout=25)
    return r.status_code, r.text

# ---------- Captcha Playwright Session ----------
class CaptchaSession:
    def __init__(self):
        self.play = None
        self.browser = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        self.play = sync_playwright().start()
        self.browser = self.play.chromium.launch(headless=True)
        self.ctx = self.browser.new_context()
        self.page = self.ctx.new_page()
        self.page.goto(REFERER, wait_until="domcontentloaded", timeout=45000)
        # wait for captcha bits
        try: self.page.wait_for_selector("img.verify_pic", timeout=8000)
        except Exception: pass
        try: self.page.wait_for_selector("img.reload_btn", timeout=8000)
        except Exception: pass
        self.page.wait_for_timeout(600)
        return self

    def __exit__(self, exc_type, exc, tb):
        try: self.browser.close()
        except Exception: pass
        try: self.play.stop()
        except Exception: pass

    def cookie_header(self) -> str:
        cookies = self.ctx.cookies()
        pairs = []
        for c in cookies:
            domain = (c.get("domain") or "").lstrip(".")
            if domain.endswith("wos-giftcode.centurygame.com"):
                pairs.append(f"{c['name']}={c['value']}")
        return "; ".join(pairs)

    def captcha_bytes(self) -> bytes | None:
        try:
            el = self.page.query_selector("img.verify_pic") or self.page.query_selector("div.verify_pic_con img")
            if el:
                src = el.get_attribute("src") or ""
                if src.startswith("data:image"):
                    b64 = src.split(",", 1)[1]
                    return base64.b64decode(b64)
                return el.screenshot(type="png")
        except Exception:
            pass
        try:
            return self.page.screenshot(type="png")
        except Exception:
            return None

    def refresh_captcha(self):
        try:
            pic = self.page.query_selector("img.verify_pic")
            old_src = pic.get_attribute("src") if pic else None
            btn = self.page.query_selector("img.reload_btn") or self.page.query_selector(".reload_btn")
            if btn:
                btn.click()
                if old_src:
                    self.page.wait_for_function(
                        "(sel, oldSrc) => { const el=document.querySelector(sel); return el && el.src && el.src !== oldSrc; }",
                        arg=("img.verify_pic", old_src),
                        timeout=3000
                    )
                else:
                    self.page.wait_for_timeout(800)
                return
        except Exception:
            pass
        try:
            self.page.reload(wait_until="domcontentloaded")
            self.page.wait_for_selector("img.verify_pic", timeout=6000)
        except Exception:
            self.page.wait_for_timeout(800)

# ---------- OCR helpers ----------
def tesseract_read(png: bytes) -> str:
    if not (pytesseract and Image and ImageOps and ImageFilter) or not png:
        return ""
    img = Image.open(io.BytesIO(png)).convert("L")
    variants = [img]
    variants.append(ImageOps.autocontrast(img).filter(ImageFilter.MedianFilter(size=3)).point(lambda x: 0 if x < 150 else 255, mode='1'))
    variants.append(ImageOps.autocontrast(img.filter(ImageFilter.SMOOTH_MORE)))
    for im in variants:
        try:
            txt = pytesseract.image_to_string(
                im, config='--psm 8 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            )
            s = re.sub(r'[^A-Za-z0-9]','', txt or '').upper()
            if 4 <= len(s) <= 8: return s
        except Exception:
            pass
    return ""

_EASY = None
def easyocr_read(png: bytes) -> str:
    global _EASY
    if not (easyocr and png):
        return ""
    try:
        if _EASY is None:
            _EASY = easyocr.Reader(['en'], gpu=False, verbose=False)
        res = _EASY.readtext(io.BytesIO(png).getvalue(), detail=1)
        best, conf = "", 0.0
        for _, text, c in res:
            s = re.sub(r'[^A-Za-z0-9]','', text or '').upper()
            if 3 <= len(s) <= 8 and c > conf:
                best, conf = s, c
        return best
    except Exception:
        return ""

def solve_captcha(png: bytes) -> str:
    t = tesseract_read(png)
    e = easyocr_read(png)
    if DEBUG: print(f"[DBG] OCR tesseract={t!r} easyocr={e!r}")
    # prefer near-agreement or longer result
    if t and e and len(t) == len(e) and sum(1 for a,b in zip(t,e) if a==b) >= max(3, len(t)-1):
        return t
    cand = max([t, e], key=lambda s: (len(s or ""), s is not None))
    return (cand or "")[:6]

def get_working_captcha(sess: CaptchaSession, max_refresh=4) -> str:
    for _ in range(max_refresh+1):
        png = sess.captcha_bytes()
        code = solve_captcha(png) if png else ""
        if 4 <= len(code) <= 8:
            return code
        sess.refresh_captcha()
    return ""

# ================= MAIN =================
post_message(STATE_CH, "🟢 WOS daily run (free OCR) starting…")

had_unhandled_error = False
error_summary = ""

try:
    bot_id = get_me()
    prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
    last_codes = int(state.get("last_id_codes") or 0)
    last_ids   = int(state.get("last_id_ids") or 0)
    roster     = state.get("roster") or {}   # {fid: {nickname, stove, updated_at}}

    # ---- read codes since last checkpoint
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
                except Exception:
                    pass
        last_codes = max(last_codes, int(m.get("id", last_codes)))

    # ---- read fids since last checkpoint
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
                except Exception:
                    pass
        last_ids = max(last_ids, int(m.get("id", last_ids)))

    # ---- update roster
    added = []
    for fid in sorted(new_fids):
        if fid not in roster:
            roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
            added.append(fid)

    # ---- captcha session & cookie
    with CaptchaSession() as sess:
        cookie_hdr = sess.cookie_header()
        print("Cookie:", "[present]" if cookie_hdr else "[none]")

        # ---- furnace scan (no captcha)
        ok_players = 0
        furnace_ups, furnace_snap = [], []
        for fid, rec in roster.items():
            ts = str(int(time.time()))
            form = {"fid": fid, "time": ts}
            form["sign"] = sign_sorted(form, WOS_SECRET)
            hp, bp = post_form(PLAYER_URL, form, cookie=None)
            try: js = json.loads(bp)
            except Exception: js = {}
            if hp == 200 and js.get("msg") == "success":
                ok_players += 1
                d = js.get("data", {})
                nick, stove = d.get("nickname"), d.get("stove_lv")
                prev = rec.get("stove")
                rec.update({"nickname": nick, "stove": stove, "updated_at": int(time.time())})
                try:
                    if prev is not None and stove is not None and int(stove) > int(prev):
                        furnace_ups.append(f"🔥 `{fid}` {nick or ''} • {prev} ➜ {stove}")
                except Exception:
                    pass
            time.sleep(0.10)

        if not furnace_ups:
            for fid, rec in roster.items():
                if rec.get("stove") is not None:
                    furnace_snap.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
            furnace_snap.sort()

        # ---- initial captcha
        CAPTCHA = get_working_captcha(sess)
        if DEBUG:
            post_message(STATE_CH, f"OCR bootstrap: {'OK' if CAPTCHA else 'FAIL'} ({len(CAPTCHA)})")

        # ---- redeem
        ok_redeems = fail_redeems = 0
        redeem_lines = []

        def try_redeem(fid: str, code: str, captcha: str):
            ts_ms = str(int(time.time() * 1000))
            form  = {
                "fid": fid,
                "cdk": code,
                "captcha_code": captcha,
                "time": ts_ms,
                "sign": sign_with_captcha(fid, code, captcha, ts_ms),
            }
            http, body = post_form(GIFTCODE_URL, form, cookie_hdr or None)
            try:
                rg = json.loads(body); msg = (rg.get("msg") or "").upper()
            except Exception:
                msg = (body or "")[:100].upper()
            return http, msg

        for code in sorted(codes):
            safe = code[:3]+"…" if len(code)>3 else code
            for fid in sorted(roster.keys()):
                status = "UNKNOWN"
                # up to 3 captcha refresh attempts per (fid, code)
                for _ in range(3):
                    if not CAPTCHA:
                        status = "NO_CAPTCHA"; break
                    http, msg = try_redeem(fid, code, CAPTCHA)

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
                    elif http == 429 or "TOO MANY" in msg:
                        status = "RATE_LIMIT"
                        time.sleep(2.5 + random.uniform(0,0.9))
                        continue
                    elif "SIGN" in msg or "PARAMS" in msg or http == 405:
                        status = "CAPTCHA_RETRY"
                        sess.refresh_captcha()
                        CAPTCHA = get_working_captcha(sess)
                        continue
                    else:
                        status = msg or f"HTTP_{http}"
                        break

                ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
                ok_redeems += int(ok); fail_redeems += int(not ok)
                redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe} • {status}")
                time.sleep(REDEEM_PACING + random.uniform(0,0.4))

    # ---- save state to Discord
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

# ---- summary message (always try)
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
    f"OCR: {'ON' if (pytesseract or easyocr) else 'OFF'}\n"
)
if had_unhandled_error and error_summary:
    summary += f"\n⚠️ {error_summary}"

post_message(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))
sys.exit(0)
