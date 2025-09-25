#!/usr/bin/env python3
import os, sys, json, re, time, io, base64, random, traceback
import requests
from urllib.parse import urlencode
from PIL import Image
try:
    import easyocr
except Exception:
    easyocr = None

# Playwright (browser automation)
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ===================== CONFIG =====================
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()  # kept for furnace API
DEBUG      = os.environ.get("DEBUG","0") == "1"

# Pacing between redemptions to stay polite
REDEEM_PACING = float(os.environ.get("REDEEM_PACING_SECONDS", "1.2"))

# Gift code site
WOS_URL   = "https://wos-giftcode.centurygame.com"
PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"  # for furnace scan (no captcha)

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing one of: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, DISCORD_STATE_CHANNEL_ID, WOS_SECRET")
    sys.exit(0)  # soft exit to keep GH Actions green while iterating

# ===================== DISCORD REST =====================
D_API = "https://discord.com/api/v10"
D_HDR = {"Authorization": f"Bot {BOT_TOKEN}"}

def d_post(channel_id, content):
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers={**D_HDR, "Content-Type":"application/json"},
                      json={"content": content[:1900]}, timeout=20)
    if not r.ok:
        print(f"[ERR] Discord POST ch={channel_id} status={r.status_code} body={r.text[:200]}")
    return r.status_code

def d_get_after(channel_id, after_id=None, limit=100):
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

def d_post_file(channel_id, content, filename, bytes_data):
    payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
    files = {'files[0]': (filename, bytes_data, 'application/json')}
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers=D_HDR,
                      data={"payload_json": json.dumps(payload)},
                      files=files, timeout=30)
    if not r.ok:
        print(f"[ERR] Discord POST file ch={channel_id} status={r.status_code} body={r.text[:200]}")
    return r.json() if r.ok else {}

def d_delete(channel_id, message_id):
    try:
        requests.delete(f"{D_API}/channels/{channel_id}/messages/{message_id}", headers=D_HDR, timeout=15)
    except Exception:
        pass

def d_me():
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

# ===================== INPUT PARSERS =====================
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

# ===================== FURNACE SCAN (no captcha) =====================
def md5(s:str):
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + secret)

def fetch_player(fid: str) -> dict:
    ts = str(int(time.time()))
    payload = {"fid": fid, "time": ts, "sign": sign_sorted({"fid": fid, "time": ts}, WOS_SECRET)}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": WOS_URL,
        "Referer": f"{WOS_URL}/",
        "User-Agent": "Mozilla/5.0",
    }
    r = requests.post(PLAYER_URL, headers=headers, data=urlencode(payload), timeout=20)
    try:
        return r.json()
    except Exception:
        return {}

# ===================== OCR (free) =====================
_EASY = None
def ocr_init():
    global _EASY
    if easyocr and _EASY is None:
        # CPU only
        _EASY = easyocr.Reader(['en'], gpu=False, verbose=False)

def ocr_read(bimg: bytes) -> str:
    """Return best A-Z0-9 guess (length 4-8), uppercase; else ''."""
    if not easyocr or _EASY is None or not bimg:
        return ""
    try:
        res = _EASY.readtext(bimg, detail=1)
        best, conf = "", -1.0
        for _, text, c in res:
            s = re.sub(r'[^A-Za-z0-9]', '', text or '').upper()
            if 3 <= len(s) <= 8 and c > conf:
                best, conf = s, float(c)
        return best
    except Exception:
        return ""

# ===================== BROWSER REDEEM FLOW =====================
class Redeemer:
    """
    One persistent Chromium page:
      - For each (fid, code), fill Player ID -> Login, wait for captcha to appear.
      - Type code + OCR captcha, click Confirm.
      - Watch network for /api/gift_code response or toast text in DOM.
    """
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
        self.page.goto(WOS_URL, wait_until="domcontentloaded", timeout=45000)
        return self

    def __exit__(self, exc_type, exc, tb):
        try: self.browser.close()
        except: pass
        try: self.play.stop()
        except: pass

    # ---- helpers ----
    def _wait_for_login_ui(self):
        # presence of "Player ID" field and "Login" button
        self.page.get_by_placeholder("Player ID").wait_for(timeout=6000)
        self.page.get_by_role("button", name=re.compile(r"login", re.I)).wait_for(timeout=6000)

    def _login_with_fid(self, fid: str):
        self._wait_for_login_ui()
        self.page.get_by_placeholder("Player ID").fill("")  # clear
        self.page.get_by_placeholder("Player ID").type(fid, delay=30)
        self.page.get_by_role("button", name=re.compile(r"login", re.I)).click()
        # After login, captcha UI should appear: code input + captcha img + confirm
        # Wait for either the avatar block or the captcha row.
        self.page.wait_for_timeout(400)  # brief UI settle
        # Wait until captcha image is visible (appears only after login)
        try:
            self.page.wait_for_selector("img.verify_pic", timeout=6000)
        except PWTimeout:
            # Some builds use container class
            self.page.wait_for_selector("div.verify_pic_con img", timeout=6000)

    def _get_captcha_png(self) -> bytes:
        el = self.page.query_selector("img.verify_pic") or self.page.query_selector("div.verify_pic_con img")
        if not el:
            return b""
        src = el.get_attribute("src") or ""
        if src.startswith("data:image"):
            try:
                b64 = src.split(",",1)[1]
                return base64.b64decode(b64)
            except Exception:
                pass
        # fallback to element screenshot
        try:
            return el.screenshot(type="png")
        except Exception:
            return b""

    def _refresh_captcha(self):
        # reload icon near captcha
        btn = self.page.query_selector("img.reload_btn") or self.page.query_selector(".reload_btn")
        if btn:
            btn.click()
            self.page.wait_for_timeout(500)
            return
        # fallback, press the page refresh (keeps context cookies)
        self.page.reload(wait_until="domcontentloaded")
        # we need to click login again to show captcha UI if we reloaded
        # (site keeps last FID, so just press Login)
        try:
            self.page.get_by_role("button", name=re.compile(r"login", re.I)).click()
            self.page.wait_for_selector("img.verify_pic", timeout=6000)
        except Exception:
            pass

    def redeem_one(self, fid: str, code: str) -> str:
        """
        Returns status string:
           SUCCESS | ALREADY | SAME_TYPE | INVALID | EXPIRED |
           CAPTCHA_RETRY | NO_LOGIN | NO_RESPONSE | ERROR:<msg>
        """
        status = "UNKNOWN"
        try:
            # 1) Ensure we're on landing and login with FID to bring captcha UI
            self.page.goto(WOS_URL, wait_until="domcontentloaded", timeout=45000)
            self._login_with_fid(fid)

            # 2) Fill gift code
            self.page.get_by_placeholder(re.compile(r"Enter Gift Code", re.I)).fill("")
            self.page.get_by_placeholder(re.compile(r"Enter Gift Code", re.I)).type(code, delay=20)

            # 3) Solve captcha (few tries)
            for attempt in range(4):
                png = self._get_captcha_png()
                guess = ocr_read(png)
                if not guess or len(guess) < 4:
                    self._refresh_captcha()
                    continue

                # fill captcha input
                cap_input = self.page.get_by_placeholder(re.compile(r"verification", re.I))
                cap_input.fill("")
                cap_input.type(guess, delay=20)

                # Watch network for gift_code call to extract its JSON (best signal)
                gift_resp = None
                def _watch(route):
                    nonlocal gift_resp
                    if "/api/gift_code" in route.url:
                        gift_resp = route
                # Click confirm
                self.page.get_by_role("button", name=re.compile(r"confirm", re.I)).click()
                # Wait some time for any toast/network
                self.page.wait_for_timeout(1200)

                # Try to capture response via network events
                # (Playwright 1.40+ has event page.on("response"), but simple poll on dom + try fetch last response)
                # We fallback to reading DOM message banners (if any).
                try:
                    # Look for a visible toast text or dialog within page
                    txt = ""
                    # try any div with error/success hints
                    for sel in [".tips_text", ".toast", ".el-message__content", ".el-message", ".msg", "body"]:
                        try:
                            el = self.page.query_selector(sel)
                            if el:
                                t = (el.text_content() or "").strip()
                                if t:
                                    txt = t
                                    break
                        except Exception:
                            pass

                    # Normalize message
                    U = (txt or "").upper()
                    if "SUCCESS" in U:
                        status = "SUCCESS"; break
                    if "RECEIVED" in U:
                        status = "ALREADY"; break
                    if "SAME TYPE" in U:
                        status = "SAME_TYPE"; break
                    if "CDK NOT FOUND" in U:
                        status = "INVALID"; break
                    if "TIME ERROR" in U or "EXPIRED" in U:
                        status = "EXPIRED"; break
                    if "CAPTCHA" in U or "VERIFY" in U:
                        status = "CAPTCHA_RETRY"
                        self._refresh_captcha()
                        continue
                    if "LOGIN" in U:
                        status = "NO_LOGIN"; break

                    # If we saw nothing, assume retry captcha first, then no response
                    if attempt < 3:
                        status = "CAPTCHA_RETRY"
                        self._refresh_captcha(); continue
                    else:
                        status = "NO_RESPONSE"
                except Exception:
                    status = "ERROR:DOM_PARSE"
                break

        except PWTimeout:
            status = "NO_LOGIN"
        except Exception as e:
            status = f"ERROR:{type(e).__name__}"
            if DEBUG:
                traceback.print_exc()

        return status

# ===================== MAIN =====================
def main():
    d_post(STATE_CH, "🟢 WOS daily run (browser-based redeem) starting…")
    had_unhandled_error = False
    error_summary = ""

    try:
        bot_id = d_me()
        prev_state_msg_id, state = find_latest_state_message(STATE_CH, bot_id)
        last_codes = int(state.get("last_id_codes") or 0)
        last_ids   = int(state.get("last_id_ids") or 0)
        roster     = state.get("roster") or {}   # {fid: {nickname, stove, updated_at}}

        # ---- read codes ----
        msgs_codes = d_get_after(CODES_CH, last_codes)
        codes = set()
        for m in msgs_codes:
            txt = m.get("content","") or ""
            codes |= parse_codes(txt)
            for t in fetch_text_attachments(m):
                codes |= parse_codes(t)
            last_codes = max(last_codes, int(m["id"]))

        # ---- read fids ----
        msgs_ids = d_get_after(IDS_CH, last_ids)
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

        # ---- furnace scan (lightweight; no captcha) ----
        ok_players = 0
        furnace_ups, furnace_snap = [], []
        for fid, rec in roster.items():
            js = fetch_player(fid)
            if js.get("msg") == "success":
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

        # ---- redeem via browser automation ----
        ocr_init()
        ok_redeems = fail_redeems = 0
        redeem_lines = []

        if codes:
            with Redeemer() as R:
                for code in sorted(codes):
                    safe = code[:3]+"…" if len(code)>3 else code
                    for fid in sorted(roster.keys()):
                        status = R.redeem_one(fid, code)
                        ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
                        ok_redeems += int(ok); fail_redeems += int(not ok)
                        redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe} • {status}")
                        time.sleep(REDEEM_PACING + random.uniform(0,0.35))

        # ---- save state back ----
        new_state = {
            "last_id_codes": str(last_codes),
            "last_id_ids":   str(last_ids),
            "roster":        roster,
            "ts":            int(time.time()),
        }
        state_bytes = json.dumps(new_state, indent=2).encode("utf-8")
        msg = d_post_file(STATE_CH, "WOSBOT_STATE v3 (do not delete)", "wos_state.json", state_bytes)
        if msg and "id" in msg and prev_state_msg_id:
            d_delete(STATE_CH, prev_state_msg_id)

        # ---- summary ----
        parts = []
        if added:
            parts.append("**New IDs added**\n" + ", ".join(f"`{a}`" for a in added[:20]) + (" …" if len(added)>20 else ""))
        if codes:
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
            f"OCR: {'ON' if easyocr else 'OFF'}\n"
        )
        d_post(STATE_CH, summary + ("\n" + "\n\n".join(parts) if parts else "\n(No changes)"))

    except Exception as e:
        had_unhandled_error = True
        error_summary = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        d_post(STATE_CH, f"**Daily Batch Summary**\nPlayers checked: 0\nRedeems: 0 ok / 0 failed\n\n⚠️ {error_summary}")

if __name__ == "__main__":
    main()
