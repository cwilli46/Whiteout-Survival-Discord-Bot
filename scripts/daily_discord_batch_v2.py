# scripts/daily_discord_batch_v2.py

import os, sys, json, re, time, hashlib, requests, traceback
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# ========== CONFIG ==========
BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CODES_CH   = os.environ.get("DISCORD_CODES_CHANNEL_ID", "").strip()
IDS_CH     = os.environ.get("DISCORD_IDS_CHANNEL_ID", "").strip()
STATE_CH   = os.environ.get("DISCORD_STATE_CHANNEL_ID", "").strip()   # also used for summaries
WOS_SECRET = os.environ.get("WOS_SECRET", "").strip()
DEBUG      = os.environ.get("DEBUG","0") == "1"

if not all([BOT_TOKEN, CODES_CH, IDS_CH, STATE_CH, WOS_SECRET]):
    print("Missing one of: DISCORD_BOT_TOKEN, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID, DISCORD_STATE_CHANNEL_ID, WOS_SECRET")
    sys.exit(0)  # keep green while iterating

def _short(s: str, n: int = 160) -> str:
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
        print(f"[ERR] Discord POST {r.status_code} {_short(r.text)}")
    return r.status_code

def get_messages_after(channel_id, after_id=None, limit=100):
    params = {"limit": limit}
    if after_id: params["after"] = str(after_id)
    r = requests.get(f"{D_API}/channels/{channel_id}/messages", headers=D_HDR, params=params, timeout=25)
    if not r.ok:
        print(f"[ERR] Discord GET {r.status_code} {_short(r.text)}")
        return []
    try:
        return r.json()
    except Exception:
        print(f"[ERR] Discord GET JSON parse failed")
        return []

def post_message_with_file(channel_id, content, filename, bytes_data):
    payload = {"content": content, "attachments":[{"id":0,"filename":filename}]}
    files = {'files[0]': (filename, bytes_data, 'application/json')}
    r = requests.post(f"{D_API}/channels/{channel_id}/messages",
                      headers=D_HDR, data={"payload_json": json.dumps(payload)},
                      files=files, timeout=40)
    if not r.ok:
        print(f"[ERR] Discord POST file {r.status_code} {_short(r.text)}")
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

# ---- flexible signer covering multiple server recipes ----
def build_sign(
    fid: str, cdk: str, ts_value: str, secret: str,
    order: str = "fixed", concat: str = "plain", uppercase: bool = False,
    include_lang: bool = False, order_seq: list | None = None
):
    """
    order: 'fixed' keeps (fid, cdk, time[, lang]),
           'sorted' alphabetical over keys,
           or provide order_seq like ['cdk','fid','time'].
    concat: 'plain' -> + SECRET
            'amp'   -> + '&' + SECRET
            'key'   -> + '&key=' + SECRET
            'prefix'-> SECRET + base
    """
    pieces = {"fid": fid, "cdk": cdk, "time": ts_value}
    if include_lang:
        pieces["lang"] = "en"

    if order_seq:
        items = [(k, pieces[k]) for k in order_seq if k in pieces]
        if "lang" in pieces: items.append(("lang", "en"))
    elif order == "sorted":
        items = sorted(pieces.items())
    else:
        items = [("fid", fid), ("cdk", cdk), ("time", ts_value)]
        if "lang" in pieces: items.append(("lang", "en"))

    base = "&".join(f"{k}={v}" for k, v in items)

    if   concat == "amp":    s = base + "&" + secret
    elif concat == "key":    s = base + "&key=" + secret
    elif concat == "prefix": s = secret + base
    else:                    s = base + secret

    d = hashlib.md5(s.encode("utf-8")).hexdigest()
    return d.upper() if uppercase else d

# ========== Playwright helpers ==========
def new_browser_context(p):
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ctx = p.chromium.launch(headless=True).new_context(user_agent=ua)
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = ctx.new_page()
    page.goto(WEB_REFERER, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2500)
    try:
        page.goto(GIFTCODE_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1200)
    except Exception:
        pass
    return ctx, page

def fetch_from_page(page, url, method, payload):
    script = """
      async ({url, method, payload}) => {
        const enc = new URLSearchParams(payload).toString();
        const init = {
          method,
          credentials: 'include',
          headers: {'X-Requested-With':'XMLHttpRequest'}
        };
        if (method === 'POST') {
          init.headers['Content-Type'] = 'application/x-www-form-urlencoded';
          init.body = enc;
        } else {
          url = url + (url.includes('?') ? '&' : '?') + enc;
        }
        const res = await fetch(url, init);
        const text = await res.text();
        return {status: res.status, text};
      }
    """
    return page.evaluate(script, {"url": url, "method": method, "payload": payload})

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

    # ---- player scan ----
    def post_form(url: str, form: dict):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_REFERER,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = requests.post(url, headers=headers, data=urlencode(form), timeout=25)
        return r.status_code, r.text

    ok_players = 0
    furnace_ups = []
    for fid, rec in roster.items():
        ts = str(int(time.time()))
        sign_p = sign_sorted({"fid": fid, "time": ts}, WOS_SECRET)
        hp, bp = post_form(PLAYER_URL, {"fid": fid, "time": ts, "sign": sign_p})
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
        time.sleep(0.08)

    # ---- snapshot if no ups ----
    furnace_snapshot = []
    if not furnace_ups:
        for fid, rec in roster.items():
            if rec.get("stove") is not None:
                furnace_snapshot.append(f"`{fid}` • L{rec['stove']} {rec.get('nickname') or ''}")
        furnace_snapshot.sort()

    # ---- redeem inside browser context (beats WAF) with extended sign variants ----
    PACING = float(os.environ.get("REDEEM_PACING_SECONDS", "6.0"))
    ok_redeems = fail_redeems = 0
    redeem_lines = []

    # (method, order, concat, ts_mode, uppercase, lang, order_seq)
    BASE_VARIANTS = [
        ("POST","fixed","plain","s",  False, False, None),
        ("GET", "fixed","plain","s",  False, False, None),
        ("POST","sorted","plain","s", False, False, None),
        ("GET", "sorted","plain","s", False, False, None),
        ("POST","fixed","key",  "s",  False, False, None),
        ("POST","sorted","key","s",  False, False, None),
        ("POST","fixed","amp",  "s",  False, False, None),
        ("POST","fixed","prefix","s", False, False, None),
        ("POST","fixed","plain","ms", False, False, None),
        ("POST","fixed","plain","s",  True,  False, None),   # UPPERCASE MD5
        ("POST","fixed","plain","s",  False, True,  None),   # include lang=en
        # explicit param sequences some services use
        ("POST","seq",  "plain","s",  False, False, ["cdk","fid","time"]),
        ("POST","seq",  "plain","s",  False, False, ["time","fid","cdk"]),
        ("GET", "seq",  "plain","s",  False, False, ["cdk","fid","time"]),
    ]

    with sync_playwright() as p:
        ctx, page = new_browser_context(p)

        for code in sorted(codes):
            safe_code = code[:3]+"…" if len(code)>3 else code
            for fid in sorted(roster.keys()):
                status = "UNKNOWN"

                for (method, order, concat, ts_mode, upper, lang, order_seq) in BASE_VARIANTS:
                    ts_value = str(int(time.time()*1000)) if ts_mode=="ms" else str(int(time.time()))
                    # translate 'seq' into fixed sequence
                    seq = order_seq if order == "seq" else None
                    sign = build_sign(fid, code, ts_value, WOS_SECRET,
                                      order=order, concat=concat, uppercase=upper,
                                      include_lang=lang, order_seq=seq)
                    payload = {"fid": fid, "cdk": code, "time": ts_value, "sign": sign}
                    if lang: payload["lang"] = "en"

                    # backoff for 429/403
                    for delay in (0, 2, 4, 8, 16):
                        if delay: time.sleep(delay)
                        res = fetch_from_page(page, GIFTCODE_URL, method, payload)
                        http = int(res.get("status") or 0)
                        bodytxt = res.get("text") or ""
                        try:
                            js = json.loads(bodytxt); msg = (js.get("msg") or "").upper()
                        except Exception:
                            msg = (bodytxt or "")[:120].upper()

                        if DEBUG:
                            print(f"[DBG] {fid} {code} {method}/{order}/{concat}/{ts_mode}"
                                  f"{'/UPPER' if upper else ''}{'/LANG' if lang else ''}"
                                  f"{'/SEQ' if seq else ''} HTTP={http} :: {msg or _short(bodytxt)}")

                        if http in (429, 403):
                            continue  # keep backing off

                        if http == 405:
                            # flip method and retry once immediately
                            method = "GET" if method == "POST" else "POST"
                            continue

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
                            status = "SIGN_ERROR" if "SIGN" in msg else "PARAMS_ERROR"
                            # try next variant
                        else:
                            status = msg or f"HTTP_{http}"
                        break  # exit backoff loop

                    if status in ("SUCCESS","ALREADY","SAME_TYPE"):
                        break  # stop trying variants for this fid/code

                ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
                ok_redeems += int(ok); fail_redeems += int(not ok)
                redeem_lines.append(f"{'✅' if ok else '❌'} {fid} • {safe_code} • {status}")
                time.sleep(PACING)

        try:
            ctx.browser.close()
        except Exception:
            pass

    # ---- save checkpoint/state back to Discord ----
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
