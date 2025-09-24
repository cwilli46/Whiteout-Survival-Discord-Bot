import os, sys, json, re, time, io, base64, hashlib, traceback, random
import requests
from urllib.parse import urlencode
from PIL import Image, ImageOps, ImageFilter
try:
    import pytesseract
except Exception:
    pytesseract = None

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ===================== CONFIG (env) =====================
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()  # also used for summaries
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()
REDEEM_PACING = float(os.environ.get("REDEEM_PACING_SECONDS", "1.4"))
DEBUG = os.environ.get("DEBUG","0") == "1"

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing required envs"); sys.exit(0)  # soft exit keeps workflow green

# ===================== DISCORD REST =====================
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def post_message(channel_id, content):
    try:
        r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                          headers={**D_HDR,"Content-Type":"application/json"},
                          json={"content": content[:1900]}, timeout=20)
        if not r.ok: print("[ERR] Discord POST", r.status_code, r.text[:200])
        return r.status_code
    except Exception as e:
        print("[ERR] Discord POST exception", e)
        return 0

def get_messages_after(channel_id, after_id=None, limit=100):
    params = {"limit": limit}
    if after_id: params["after"] = str(after_id)
    try:
        r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=20)
        if not r.ok: return []
        return r.json()
    except Exception:
        return []

def latest_messages(channel_id, limit=25):
    try:
        r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params={"limit": limit}, timeout=20)
        if not r.ok: return []
        return r.json()
    except Exception:
        return []

def post_message_with_file(channel_id, content, filename, bytes_data):
    payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
    files = {'files[0]': (filename, bytes_data, 'application/json')}
    try:
        r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                          headers=D_HDR, data={"payload_json": json.dumps(payload)}, files=files, timeout=30)
        return r.json() if r.ok else {}
    except Exception:
        return {}

def delete_message(channel_id, message_id):
    try: requests.delete(f"{D_API}/channels/{channel_id}/messages/{message_id}", headers=D_HDR, timeout=15)
    except Exception: pass

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
                except Exception:
                    return m["id"], {}
    return None, {}

# ===================== PARSERS =====================
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

# ===================== WOS API (for furnace scan) =====================
PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
ORIGIN       = "https://wos-giftcode.centurygame.com"
REFERER      = "https://wos-giftcode.centurygame.com/"

def md5(s:str) -> str: return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
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

# ===================== OCR helpers (Tesseract only) =====================
def ocr_captcha(png: bytes) -> str:
    if not (pytesseract and png): return ""
    img = Image.open(io.BytesIO(png)).convert("L")
    candidates = []

    # 1) raw-ish
    candidates.append(img)

    # 2) hard threshold after autocontrast
    v = ImageOps.autocontrast(img)
    candidates.append(v.point(lambda x: 0 if x < 150 else 255, mode='1'))

    # 3) smooth + autocontrast
    candidates.append(ImageOps.autocontrast(img.filter(ImageFilter.SMOOTH_MORE)))

    for im in candidates:
        try:
            txt = pytesseract.image_to_string(
                im, config='--psm 8 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            )
            s = re.sub(r'[^A-Za-z0-9]', '', txt or '').upper()
            if 4 <= len(s) <= 8:
                return s[:6]
        except Exception:
            pass
    return ""

# ===================== Playwright browser flow =====================
class Redeemer:
    def __init__(self):
        self.play = None
        self.browser = None

    def __enter__(self):
        self.play = sync_playwright().start()
        self.browser = self.play.chromium.launch(headless=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        try: self.browser.close()
        except: pass
        try: self.play.stop()
        except: pass

    def new_context(self):
        ctx = self.browser.new_context()
        page = ctx.new_page()
        page.goto(REFERER, wait_until="domcontentloaded", timeout=45000)
        return ctx, page

    @staticmethod
    def _get_base64_or_screenshot(page):
        # Return bytes for the captcha image (prefer base64 data URI from <img.verify_pic>)
        try:
            el = page.query_selector("img.verify_pic") or page.query_selector("div.verify_pic_con img")
            if el:
                src = el.get_attribute("src") or ""
                if src.startswith("data:image"):
                    b64 = src.split(",", 1)[1]
                    return base64.b64decode(b64)
                return el.screenshot(type="png")
        except Exception:
            pass
        return None

    @staticmethod
    def _refresh_captcha(page):
        try:
            img = page.query_selector("img.verify_pic")
            old_src = img.get_attribute("src") if img else None
            btn = page.query_selector("img.reload_btn") or page.query_selector(".reload_btn")
            if btn:
                btn.click()
                if old_src:
                    try:
                        page.wait_for_function(
                            "(os)=>{const el=document.querySelector('img.verify_pic'); return el && el.src && el.src!==os;}",
                            arg=old_src, timeout=3000
                        )
                    except Exception:
                        page.wait_for_timeout(500)
                else:
                    page.wait_for_timeout(600)
            else:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("img.verify_pic", timeout=6000)
        except Exception:
            page.wait_for_timeout(500)

    @staticmethod
    def _fill_by_label(page, placeholder_regex, fallback_selector):
        try:
            el = page.get_by_placeholder(placeholder_regex)
            el.wait_for(timeout=2000)
            return el
        except Exception:
            el = page.locator(fallback_selector).first
            el.wait_for(timeout=2000)
            return el

    def login_fid(self, page, fid: str) -> bool:
        # type FID and press Login; wait until captcha appears
        try:
            fid_input = self._fill_by_label(page, re.compile("player id", re.I), "input")
            fid_input.fill("")
            fid_input.type(fid, delay=40)
            page.get_by_role("button", name=re.compile("login", re.I)).click()
        except Exception:
            # try click by text
            try:
                page.locator("text=Login").click()
            except Exception:
                pass
        # wait for captcha image to appear (this indicates login step was accepted)
        try:
            page.wait_for_selector("img.verify_pic", timeout=8000)
            return True
        except PWTimeout:
            return False

    def redeem_once(self, page, fid: str, code: str) -> tuple[int, str]:
        """Submit the form (after login) with an OCR captcha and return (http_code, UPPER_MSG)."""
        # Fill gift code
        try:
            code_input = self._fill_by_label(page, re.compile("gift code", re.I), "div.code_con input")
            code_input.fill("")
            code_input.type(code, delay=25)
        except Exception:
            pass

        # OCR captcha
        png = self._get_base64_or_screenshot(page)
        captcha = ocr_captcha(png) if png else ""
        if DEBUG: print(f"[DBG] OCR={captcha}")

        # Fill captcha input
        try:
            cap_input = page.locator("div.verify_con input").first
            cap_input.fill("")
            cap_input.type(captcha or "", delay=25)
        except Exception:
            pass

        # Click Confirm and capture /api/gift_code response
        resp = None
        try:
            with page.expect_response(lambda r: "/api/gift_code" in r.url, timeout=10000) as wait_resp:
                page.get_by_role("button", name=re.compile("confirm", re.I)).click()
            resp = wait_resp.value
        except Exception:
            # no response caught, treat as unknown
            return 0, "NO_RESPONSE"

        code_http = 0
        msg_upper = "UNKNOWN"
        try:
            code_http = resp.status
            data = resp.json()
            msg_upper = ((data or {}).get("msg") or "").upper()
        except Exception:
            try:
                txt = resp.text()
                msg_upper = (txt or "")[:100].upper()
            except Exception:
                pass

        return code_http, msg_upper

# ===================== MAIN =====================
post_message(STATE_CH, "🟢 WOS daily run (browser login + free OCR) starting…")

had_unhandled_error = False
error_summary = ""

try:
    # ---- load state from Discord ----
    bot_id = get_me()
    prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
    last_codes = int(state.get("last_id_codes") or 0)
    last_ids   = int(state.get("last_id_ids") or 0)
    roster     = state.get("roster") or {}   # {fid: {nickname, stove, updated_at}}

    # ---- read codes since last checkpoint ----
    msgs_codes = get_messages_after(CODES_CH, last_codes)
    codes = set()
    for m in msgs_codes:
        txt = m.get("content","") or ""
        codes |= parse_codes(txt)
        # attachments
        for att in m.get("attachments", []):
            n = att.get("filename","").lower()
            if any(n.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
                try:
                    r = requests.get(att["url"], timeout=20)
                    if r.ok: codes |= parse_codes(r.text)
                except Exception: pass
        last_codes = max(last_codes, int(m["id"]))

    # ---- read fids since last checkpoint ----
    msgs_ids = get_messages_after(IDS_CH, last_ids)
    new_fids = set()
    for m in msgs_ids:
        txt = m.get("content","") or ""
        new_fids |= parse_fids(txt)
        for att in m.get("attachments", []):
            n = att.get("filename","").lower()
            if any(n.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
                try:
                    r = requests.get(att["url"], timeout=20)
                    if r.ok: new_fids |= parse_fids(r.text)
                except Exception: pass
        last_ids = max(last_ids, int(m["id"]))

    # ---- update roster ----
    added = []
    for fid in sorted(new_fids):
        if fid not in roster:
            roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
            added.append(fid)

    # ---- furnace scan (no captcha) ----
    ok_players = 0
    furnace_ups = []
    furnace_snap = []
    for fid, rec in roster.items():
        ts = str(int(time.time()))
        payload = {"fid": fid, "time": ts}
        payload["sign"] = sign_sorted(payload, WOS_SECRET)
        hp, bp = post_form(PLAYER_URL, payload, cookie=None)
        try: js = json.loads(bp)
        except Exception: js = {}

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
        time.sleep(0.08)

    if not furnace_ups:
        for fid, rec in roster.items():
            if rec.get("stove") is not None:
                furnace_snap.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
        furnace_snap.sort()

    # ---- redeem via BROWSER (login+captcha) ----
    ok_redeems = fail_redeems = 0
    redeem_lines = []

    with Redeemer() as rd:
        for code in sorted(codes):
            safe = code[:3]+"…" if len(code)>3 else code
            for fid in sorted(roster.keys()):
                status = "UNKNOWN"

                # New isolated context per FID (keeps sessions separate)
                ctx, page = rd.new_context()

                # 1) Login for the fid (this makes captcha visible and binds session)
                logged_in = rd.login_fid(page, fid)
                if not logged_in:
                    status = "NO_LOGIN"
                else:
                    # 2) Try up to 4 attempts, refreshing captcha/backing off on rate-limit or captcha errors
                    for attempt in range(4):
                        http, msg = rd.redeem_once(page, fid, code)

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
                        elif "LOGIN" in msg:
                            status = "NO_LOGIN"
                            # try relogin once
                            if rd.login_fid(page, fid): 
                                continue
                            else:
                                break
                        elif http == 429 or "TOO MANY" in msg:
                            status = "RATE_LIMIT"
                            time.sleep(1.2 + attempt*1.5 + random.uniform(0,0.7))
                            continue
                        elif "CAPTCHA" in msg or "VERIFY" in msg or "SIGN" in msg or "PARAMS" in msg:
                            status = "CAPTCHA_RETRY"
                            rd._refresh_captcha(page)
                            time.sleep(0.4 + random.uniform(0,0.3))
                            continue
                        else:
                            status = msg or f"HTTP_{http}"
                            break

                try: ctx.close()
                except Exception: pass

                ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
                ok_redeems += int(ok); fail_redeems += int(not ok)
                redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe} • {status}")

                time.sleep(REDEEM_PACING + random.uniform(0, 0.3))

    # ---- save state back as attachment ----
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

# ===================== SUMMARY =====================
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
    f"OCR: {'ON' if pytesseract else 'OFF'} (Tesseract)\n"
)
if had_unhandled_error and error_summary:
    summary += f"\n⚠️ {error_summary}"

post_message(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))
sys.exit(0)
