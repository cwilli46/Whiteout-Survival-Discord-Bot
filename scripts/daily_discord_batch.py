#!/usr/bin/env python3
import os, sys, json, re, time, io, base64, hashlib, random, traceback
import requests
from urllib.parse import urlencode
from PIL import Image, ImageOps, ImageFilter

# ========= OPTIONAL: PyTesseract (free OCR) =========
try:
    import pytesseract  # uses system tesseract-ocr (installed in workflow)
except Exception:
    pytesseract = None

# ========== CONFIG ==========
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()  # also used for summaries
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()

# Optional knobs
DEBUG               = os.environ.get("DEBUG", "0") == "1"
REDEEM_PACING       = float(os.environ.get("REDEEM_PACING_SECONDS", "1.6"))
REQUEST_TIMEOUT_S   = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "20"))

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing one of: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, DISCORD_STATE_CHANNEL_ID, WOS_SECRET")
    sys.exit(0)  # soft exit so the workflow stays green while you wire secrets

# ========== CONSTANTS ==========
BASE_API     = "https://wos-giftcode-api.centurygame.com/api"
PLAYER_URL   = f"{BASE_API}/player"
CAPTCHA_URL  = f"{BASE_API}/captcha"     # <-- called AFTER sending FID+CDK, returns/arms a captcha
GIFTCODE_URL = f"{BASE_API}/gift_code"   # <-- final redeem
ORIGIN   = "https://wos-giftcode.centurygame.com"
REFERER  = "https://wos-giftcode.centurygame.com/"

# ========== DISCORD REST ==========
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def post_message(channel_id, content):
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers={**D_HDR, "Content-Type": "application/json"},
                      json={"content": content[:1900]},
                      timeout=REQUEST_TIMEOUT_S)
    if not r.ok:
        print(f"[ERR] Discord POST ch={channel_id} status={r.status_code} body={r.text[:200]}")
    return r.status_code

def get_messages_after(channel_id, after_id=None, limit=100):
    params = {"limit": limit}
    if after_id: params["after"] = str(after_id)
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=REQUEST_TIMEOUT_S)
    if not r.ok: return []
    try: return r.json()
    except Exception: return []

def post_message_with_file(channel_id, content, filename, bytes_data):
    payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
    files = {'files[0]': (filename, bytes_data, 'application/json')}
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers=D_HDR, data={"payload_json": json.dumps(payload)}, files=files, timeout=REQUEST_TIMEOUT_S)
    if not r.ok:
        print(f"[ERR] Discord POST file ch={channel_id} status={r.status_code} body={r.text[:200]}")
    return r.json() if r.ok else {}

def delete_message(channel_id, message_id):
    try:
        requests.delete(f"{D_API}/channels/{channel_id}/messages/{message_id}", headers=D_HDR, timeout=15)
    except Exception:
        pass

def get_me():
    r = requests.get(f"{D_API}/users/@me", headers=D_HDR, timeout=REQUEST_TIMEOUT_S)
    r.raise_for_status()
    return r.json()["id"]

def find_latest_state_message(channel_id, bot_user_id):
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params={"limit": 25}, timeout=REQUEST_TIMEOUT_S)
    if not r.ok: return None, {}
    for m in r.json():  # newest first
        if str(m.get("author",{}).get("id")) != str(bot_user_id):
            continue
        for att in m.get("attachments", []):
            if att.get("filename") == "wos_state.json":
                try:
                    txt = requests.get(att["url"], timeout=REQUEST_TIMEOUT_S).text
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
    out, cur = set(), None
    for line in (text or "").splitlines():
        if YAML_CODES.match(line): cur="codes"; continue
        m = BULLET.match(line)
        if m and cur=="codes": out.add(m.group(1).strip().upper()); continue
        m2 = CSV_CODE.match(line)
        if m2: out.add(m2.group(1).upper())
    return out

def parse_fids(text):
    out, cur = set(), None
    for line in (text or "").splitlines():
        if YAML_FIDS.match(line): cur="fids"; continue
        m = BULLET.match(line)
        if m and cur=="fids": out.add(m.group(1).strip()); continue
        m2 = CSV_FID.match(line)
        if m2: out.add(m2.group(1))
    return out

def fetch_text_attachments(msg):
    texts = []
    for att in msg.get("attachments", []):
        name = att.get("filename","").lower()
        if any(name.endswith(ext) for ext in (".txt",".csv",".yml",".yaml")):
            try:
                resp = requests.get(att["url"], timeout=REQUEST_TIMEOUT_S)
                if resp.ok: texts.append(resp.text)
            except Exception:
                pass
    return texts

# ========== UTILS ==========
def md5(s:str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + secret)

# ========= HTTP session with site-like headers (keeps cookies) =========
def new_http_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
    })
    try:
        s.get(REFERER, timeout=REQUEST_TIMEOUT_S)
    except Exception:
        pass
    return s

# ========= Captcha fetch + OCR =========
def ocr_captcha(img_bytes: bytes) -> str:
    if not img_bytes or not pytesseract:
        return ""
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("L")
    except Exception:
        return ""
    variants = [im]
    v = ImageOps.autocontrast(im).filter(ImageFilter.MedianFilter(size=3))
    variants.append(v.point(lambda x: 0 if x < 150 else 255, mode="1"))
    variants.append(ImageOps.autocontrast(im.filter(ImageFilter.SMOOTH_MORE)))

    for vv in variants:
        try:
            txt = pytesseract.image_to_string(
                vv,
                config="--psm 8 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            )
            s = re.sub(r"[^A-Za-z0-9]", "", (txt or "")).upper()
            if 4 <= len(s) <= 8:
                return s
        except Exception:
            continue
    return ""

def prime_captcha_for_code(sess: requests.Session, fid: str, code: str) -> bytes | None:
    """
    FE behavior: after you enter FID + code and press Confirm, the page:
      1) posts /api/player (login)
      2) posts /api/captcha with fid+cdk(+time+sign) to ARM & RETURN the captcha image (or base64).
    We do the same here.
    """
    # Observed: captcha prime signs over {cdk, fid, time} (alphabetical)
    ts = str(int(time.time()))
    payload = {"cdk": code, "fid": fid, "time": ts}
    payload["sign"] = sign_sorted(payload, WOS_SECRET)

    try:
        r = sess.post(CAPTCHA_URL, data=payload, timeout=REQUEST_TIMEOUT_S)
    except Exception:
        return None

    # Some deployments return JSON { code:0, data:{ img: "data:image/jpeg;base64,..."} }
    # Others may return JSON with 'image'/'captcha'/'base64' fields
    # In error, they may return { code:1, msg:"CAPTCHA CHECK ERROR." }
    try:
        if (r.headers.get("Content-Type") or "").lower().startswith("application/json"):
            j = r.json()
            if DEBUG:
                print(f"[DBG] captcha json: {str(j)[:120]}")
            data = j.get("data") or {}
            # common keys
            for k in ("img", "image", "captcha", "data_url", "base64"):
                v = data if isinstance(data, str) else data.get(k)
                if not v: continue
                if isinstance(v, str):
                    if v.startswith("data:image"):
                        return base64.b64decode(v.split(",",1)[1])
                    try:
                        return base64.b64decode(v)
                    except Exception:
                        pass
            # sometimes backend returns an array: data:[{img:...}]
            if isinstance(data, list) and data:
                for it in data:
                    if isinstance(it, dict):
                        v = it.get("img") or it.get("image") or it.get("data_url")
                        if isinstance(v, str):
                            if v.startswith("data:image"):
                                return base64.b64decode(v.split(",",1)[1])
                            try:
                                return base64.b64decode(v)
                            except Exception:
                                pass
            # If not present and code says error, just return None (we'll retry)
            return None
        else:
            # Raw image bytes
            b = r.content or b""
            if b.startswith(b"\xff\xd8\xff") or b.startswith(b"\x89PNG"):
                return b
            return None
    except Exception:
        # If not JSON/parseable, try content as image
        b = r.content or b""
        if b.startswith(b"\xff\xd8\xff") or b.startswith(b"\x89PNG"):
            return b
        return None

# ========= API helpers =========
def login_fid(sess: requests.Session, fid: str) -> bool:
    ts = str(int(time.time()))
    form = {"fid": fid, "time": ts}
    form["sign"] = sign_sorted(form, WOS_SECRET)
    try:
        r = sess.post(PLAYER_URL, data=form, timeout=REQUEST_TIMEOUT_S)
        js = r.json() if r.headers.get("Content-Type","").startswith("application/json") else {}
        return r.ok and (js.get("msg") == "success")
    except Exception:
        return False

def redeem_once(sess: requests.Session, fid: str, code: str, captcha: str) -> tuple[int, str]:
    ts_ms = str(int(time.time() * 1000))
    base  = f"fid={fid}&cdk={code}&captcha_code={captcha}&time={ts_ms}"
    sign  = md5(base + WOS_SECRET)
    form = {"fid": fid, "cdk": code, "captcha_code": captcha, "time": ts_ms, "sign": sign}
    r = sess.post(GIFTCODE_URL, data=form, timeout=REQUEST_TIMEOUT_S)
    msg = f"HTTP_{r.status_code}"
    try:
        js  = r.json()
        msg = (js.get("msg") or "").upper() or msg
    except Exception:
        pass
    return r.status_code, msg

def redeem_one(sess: requests.Session, fid: str, code: str, max_tries: int = 6) -> str:
    # 1) login
    login_fid(sess, fid)

    # 2..n) prime captcha for this (fid,code), OCR, redeem
    for attempt in range(1, max_tries + 1):
        img = prime_captcha_for_code(sess, fid, code)
        if DEBUG:
            print(f"[DBG] prime_captcha fid={fid} code={code} got_img={bool(img)} size={len(img) if img else 0}")
        cap = ocr_captcha(img) if img else ""
        if not cap:
            time.sleep(0.8)
            continue

        http, msg = redeem_once(sess, fid, code, cap)

        # Success / terminal
        if "SUCCESS" in msg:         return "SUCCESS"
        if "RECEIVED" in msg:        return "ALREADY"
        if "SAME TYPE" in msg:       return "SAME_TYPE"
        if "TIME ERROR" in msg:      return "EXPIRED"
        if "CDK NOT FOUND" in msg:   return "INVALID"

        # Retryable
        if http == 429 or "TOO MANY" in msg:
            time.sleep(3 + random.uniform(0, 1.5))
            continue

        # Most common captcha-related failures -> retry with a fresh captcha
        if any(k in msg for k in ("CAPTCHA", "PARAMS", "SIGN", "CHECK ERROR")) or http in (400, 401, 405):
            time.sleep(0.9)
            continue

        # Anything else: return what we got
        return msg or f"HTTP_{http}"

    return "CAPTCHA_RETRY"

# ========== MAIN ==========
post_message(STATE_CH, "🟢 WOS daily run (free OCR, captcha priming) starting…")

had_unhandled_error = False
error_summary = ""
redeem_lines = []
furnace_ups = []
furnace_snap = []
ok_players = 0
ok_redeems = 0
fail_redeems = 0

try:
    bot_id = get_me()
    prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
    last_codes = int(state.get("last_id_codes") or 0)
    last_ids   = int(state.get("last_id_ids") or 0)
    roster     = state.get("roster") or {}   # {fid: {nickname, stove, updated_at}}

    # ---- read codes ----
    msgs_codes = get_messages_after(CODES_CH, last_codes)
    codes = set()
    for m in msgs_codes:
        codes |= parse_codes(m.get("content","") or "")
        for t in fetch_text_attachments(m):
            codes |= parse_codes(t)
        last_codes = max(last_codes, int(m["id"]))

    # ---- read fids ----
    msgs_ids = get_messages_after(IDS_CH, last_ids)
    new_fids = set()
    for m in msgs_ids:
        new_fids |= parse_fids(m.get("content","") or "")
        for t in fetch_text_attachments(m):
            new_fids |= parse_fids(t)
        last_ids = max(last_ids, int(m["id"]))

    # ---- update roster ----
    added = []
    for fid in sorted(new_fids):
        if fid not in roster:
            roster[fid] = {"nickname": None, "stove": None, "updated_at": None}
            added.append(fid)

    # ---- create session & furnace scan ----
    sess = new_http_session()

    for fid, rec in roster.items():
        ts = str(int(time.time()))
        payload = {"fid": fid, "time": ts}
        payload["sign"] = sign_sorted(payload, WOS_SECRET)
        try:
            r = sess.post(PLAYER_URL, data=payload, timeout=REQUEST_TIMEOUT_S)
            js = r.json() if r.headers.get("Content-Type","").startswith("application/json") else {}
        except Exception:
            js = {}

        if r.ok and js.get("msg") == "success":
            ok_players += 1
            d = js.get("data", {}) or {}
            nick  = d.get("nickname")
            stove = d.get("stove_lv")
            prev  = rec.get("stove")
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

    # ---- redeem (fresh captcha per FID, per code) ----
    for code in sorted(codes):
        safe = code[:3]+"…" if len(code)>3 else code
        for fid in sorted(roster.keys()):
            status = redeem_one(sess, fid, code, max_tries=6)
            ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
            ok_redeems += int(ok); fail_redeems += int(not ok)
            redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe} • {status}")
            time.sleep(REDEEM_PACING + random.uniform(0, 0.5))

    # ---- persist state back to Discord ----
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

# ========== SUMMARY ==========
parts = []
if 'added' in locals() and added:
    parts.append("**New IDs added**\n" + ", ".join(f"`{a}`" for a in added[:20]) + (" …" if len(added)>20 else ""))
if 'codes' in locals() and codes:
    parts.append("**Codes processed**\n" + ", ".join(f"`{c}`" for c in sorted(codes)))
if furnace_ups:
    parts.append("**Furnace level ups**\n" + "\n".join(furnace_ups[:15]) + (" \n…" if len(furnace_ups)>15 else ""))
elif furnace_snap:
    parts.append("**Furnace levels (snapshot)**\n" + "\n".join(furnace_snap[:15]) + (" \n…" if len(furnace_snap)>15 else ""))

if redeem_lines:
    parts.append("**Redeem results (first 25)**\n" + "\n".join(redeem_lines[:25]) + (" \n…" if len(redeem_lines)>25 else ""))

summary = (
    f"**Daily Batch Summary**\n"
    f"Players checked: {ok_players}\n"
    f"Redeems: {ok_redeems} ok / {fail_redeems} failed\n"
    f"OCR: {'ON' if pytesseract else 'OFF (install tesseract)'}\n"
)
if had_unhandled_error and error_summary:
    summary += f"\n⚠️ {error_summary}"

post_message(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))
sys.exit(0)
