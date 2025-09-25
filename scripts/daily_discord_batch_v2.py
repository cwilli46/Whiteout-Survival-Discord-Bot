#!/usr/bin/env python3
import os, sys, json, re, time, random, hashlib, traceback, io
import requests
from urllib.parse import urlencode

# --- free OCR ---
from PIL import Image, ImageOps, ImageFilter
import pytesseract

# --- browser ---
from playwright.sync_api import sync_playwright

# ========== CONFIG ==========
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()
DEBUG      = os.environ.get("DEBUG","0") == "1"
REDEEM_PACING = float(os.environ.get("REDEEM_PACING_SECONDS", "3.0"))

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing one of: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, DISCORD_STATE_CHANNEL_ID, WOS_SECRET")
    sys.exit(0)

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

def get_me():
    r = requests.get(f"{D_API}/users/@me", headers=D_HDR, timeout=15)
    r.raise_for_status()
    return r.json()["id"]

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

def md5(s:str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + secret)

# -------- OCR helpers --------
def ocr_from_png(png_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    # a couple of simple pre-process passes
    variants = []
    variants.append(img)
    variants.append(ImageOps.autocontrast(img).filter(ImageFilter.MedianFilter(size=3)))
    variants.append(ImageOps.autocontrast(img.filter(ImageFilter.SMOOTH_MORE)))
    for im in variants:
        try:
            txt = pytesseract.image_to_string(
                im,
                config='--psm 8 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            )
            s = re.sub(r'[^A-Za-z0-9]', '', txt or '').upper()
            if 4 <= len(s) <= 8:
                return s[:6]
        except Exception:
            pass
    return ""

# -------- Browser solver --------
class PageSolver:
    def __init__(self):
        self.p = None
        self.browser = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        self.p = sync_playwright().start()
        self.browser = self.p.chromium.launch(headless=True)
        self.ctx = self.browser.new_context()
        self.page = self.ctx.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        try: self.browser.close()
        except: pass
        try: self.p.stop()
        except: pass

    def redeem(self, fid: str, code: str) -> str:
        """
        Drive actual page:
        1) load
        2) fill Player ID + Login
        3) fill Gift Code + Confirm
        4) when captcha UI appears: read image -> OCR -> type -> Confirm
        5) watch /api/gift_code XHR and parse msg
        """
        page = self.page
        page.goto(REFERER, wait_until="domcontentloaded", timeout=45000)

        # Fill Player ID and login
        try:
            # by placeholder or text
            page.get_by_placeholder(re.compile("Player ID", re.I)).fill(fid)
        except Exception:
            # fallback: first input in the form
            page.locator("input").first.fill(fid)
        # click Login
        try:
            page.get_by_role("button", name=re.compile("Login", re.I)).click()
        except Exception:
            page.locator("button:has-text('Login')").click()
        page.wait_for_timeout(400)

        # Fill code
        try:
            page.get_by_placeholder(re.compile("Gift Code|Enter Gift Code", re.I)).fill(code)
        except Exception:
            page.locator("input").nth(1).fill(code)

        # Click Confirm to trigger captcha render
        try:
            page.get_by_role("button", name=re.compile("Confirm", re.I)).click()
        except Exception:
            page.locator("button:has-text('Confirm')").click()

        # Wait for captcha image to appear
        try:
            page.wait_for_selector("img.verify_pic", timeout=6000)
        except Exception:
            # Some pages render the image directly as data URL; fallback small wait
            page.wait_for_timeout(800)

        # Try up to 4 times
        for attempt in range(4):
            # Grab captcha image bytes (base64 data-url or element screenshot)
            png = None
            try:
                el = page.query_selector("img.verify_pic") or page.query_selector("div.verify_pic_con img")
                if el:
                    src = el.get_attribute("src") or ""
                    if src.startswith("data:image"):
                        b64 = src.split(",", 1)[1]
                        png = io.BytesIO()
                        png.write(base64.b64decode(b64))
                        png = png.getvalue()
                    else:
                        png = el.screenshot(type="png")
            except Exception:
                pass

            if not png:
                # last resort small wait then re-try refresh button
                page.wait_for_timeout(300)
                try:
                    page.locator("img.reload_btn, .reload_btn").click()
                except Exception:
                    pass
                continue

            guess = ocr_from_png(png)
            if DEBUG:
                print(f"[DBG] OCR guess: {guess}")

            if not guess:
                try:
                    page.locator("img.reload_btn, .reload_btn").click()
                except Exception:
                    pass
                page.wait_for_timeout(350)
                continue

            # type the captcha in its input (left of image)
            # selector is usually the only short input inside verify block
            try:
                page.locator("div.verify_con input[type='text']").fill(guess)
            except Exception:
                # fallback to any input not equal to fid/code we already filled
                inputs = page.locator("input").all()
                for inp in inputs[-3:]:
                    try:
                        val = inp.input_value()
                        if not val or val.upper() == val.lower():  # empty or not obviously code/fid
                            inp.fill(guess); break
                    except Exception:
                        continue

            # Submit again
            # Watch the API response for gift_code
            with page.expect_response(lambda r: "/api/gift_code" in r.url, timeout=8000) as resp_info:
                try:
                    page.get_by_role("button", name=re.compile("Confirm", re.I)).click()
                except Exception:
                    page.locator("button:has-text('Confirm')").click()
            resp = resp_info.value
            msg = ""
            try:
                data = resp.json()
                msg = (data.get("msg") or "").upper()
            except Exception:
                msg = (resp.text() or "")[:100].upper()

            if any(x in msg for x in ("SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE")):
                if "SUCCESS" in msg: return "SUCCESS"
                if "RECEIVED" in msg: return "ALREADY"
                return "SAME_TYPE"

            if "CAPTCHA" in msg or "PARAMS" in msg or "SIGN" in msg:
                # refresh and retry
                try:
                    page.locator("img.reload_btn, .reload_btn").click()
                except Exception:
                    pass
                page.wait_for_timeout(400)
                continue

            # other terminal status
            if "TIME ERROR" in msg:
                return "EXPIRED"
            if "CDK NOT FOUND" in msg:
                return "INVALID"

            # Unknown — try once more then give up
            page.wait_for_timeout(350)

        return "CAPTCHA_RETRY"

# ========== MAIN ==========
had_unhandled_error = False
error_summary = ""
post_message(STATE_CH, "🟢 WOS daily run (captcha solver) starting…")

try:
    # load state
    bot_id = get_me()
    prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
    last_codes = int(state.get("last_id_codes") or 0)
    last_ids   = int(state.get("last_id_ids") or 0)
    roster     = state.get("roster") or {}

    # read codes
    msgs_codes = get_messages_after(CODES_CH, last_codes)
    codes = set()
    for m in msgs_codes:
        txt = m.get("content","") or ""
        for att in m.get("attachments", []):
            name = att.get("filename","").lower()
            if any(name.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
                try:
                    r = requests.get(att["url"], timeout=20)
                    if r.ok: txt += "\n" + r.text
                except Exception:
                    pass
        codes |= parse_codes(txt)
        if "id" in m: last_codes = max(last_codes, int(m["id"]))

    # read fids
    msgs_ids = get_messages_after(IDS_CH, last_ids)
    new_fids = set()
    for m in msgs_ids:
        txt = m.get("content","") or ""
        for att in m.get("attachments", []):
            name = att.get("filename","").lower()
            if any(name.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
                try:
                    r = requests.get(att["url"], timeout=20)
                    if r.ok: txt += "\n" + r.text
                except Exception:
                    pass
        new_fids |= parse_fids(txt)
        if "id" in m: last_ids = max(last_ids, int(m["id"]))

    # update roster
    added = []
    for fid in sorted(new_fids):
        if fid not in roster:
            roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
            added.append(fid)

    # furnace scan (no captcha)
    ok_players = 0
    furnace_ups, furnace_snapshot = [], []
    def sign_player(fid, ts): return sign_sorted({"fid": fid, "time": ts}, WOS_SECRET)

    for fid, rec in roster.items():
        ts = str(int(time.time()))
        http = requests.post(PLAYER_URL,
                             headers={**BROWSER_HEADERS_BASE, "Content-Type":"application/x-www-form-urlencoded"},
                             data=urlencode({"fid": fid, "time": ts, "sign": sign_player(fid, ts)}),
                             timeout=20)
        try:
            js = http.json()
        except Exception:
            js = {}
        if http.status_code == 200 and js.get("msg") == "success":
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
        time.sleep(0.05)

    if not furnace_ups:
        for fid, rec in roster.items():
            if rec.get("stove") is not None:
                furnace_snapshot.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
        furnace_snapshot.sort()

    # redeem via browser+OCR
    ok_redeems = fail_redeems = 0
    redeem_lines = []
    with PageSolver() as solver:
        for code in sorted(codes):
            safe = code[:3] + "…" if len(code) > 3 else code
            for fid in sorted(roster.keys()):
                status = solver.redeem(fid, code)
                ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
                ok_redeems += int(ok); fail_redeems += int(not ok)
                redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe} • {status}")
                # pacing + jitter
                time.sleep(REDEEM_PACING + random.uniform(0.25, 0.75))

    # save state
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
elif 'furnace_snapshot' in locals() and furnace_snapshot:
    parts.append("**Furnace levels (snapshot)**\n" + "\n".join(furnace_snapshot[:15]) + (" \n…" if len(furnace_snapshot)>15 else ""))
if 'redeem_lines' in locals() and redeem_lines:
    parts.append("**Redeem results (first 25)**\n" + "\n".join(redeem_lines[:25]) + (" \n…" if len(redeem_lines)>25 else ""))

summary = (
    f"**Daily Batch Summary**\n"
    f"Players checked: {locals().get('ok_players',0)}\n"
    f"Redeems: {locals().get('ok_redeems',0)} ok / {locals().get('fail_redeems',0)} failed\n"
)
if had_unhandled_error and error_summary:
    summary += f"\n\n⚠️ {error_summary}"

post_message(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))
sys.exit(0)
