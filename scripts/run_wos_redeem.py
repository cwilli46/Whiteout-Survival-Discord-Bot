import os, time, hashlib, requests, json, sys
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

SECRET = os.environ.get("WOS_SECRET", "").strip()
CODE   = os.environ.get("GIFT_CODE", "").strip()
FIDS   = [x.strip() for x in os.environ.get("FIDS","").split(",") if x.strip()]

PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
ORIGIN       = "https://wos-giftcode.centurygame.com"
REFERER      = "https://wos-giftcode.centurygame.com/"

if not SECRET or not CODE or not FIDS:
    print("Missing env: WOS_SECRET / GIFT_CODE / FIDS")
    sys.exit(1)

def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + SECRET), base

def post_form(url: str, form: dict, cookie: str | None):
    headers = {
      "Content-Type": "application/x-www-form-urlencoded",
      "Origin": ORIGIN,
      "Referer": REFERER,
      "Accept": "application/json, text/plain, */*",
      "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    }
    if cookie:
        headers["Cookie"] = cookie
    resp = requests.post(url, headers=headers, data=urlencode(form), timeout=20)
    return resp.status_code, resp.text

# Get a normal browser cookie (helps bypass WAF/captcha)
print("Launching headless Chromium to collect session cookie…")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(REFERER, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3000)
    cookies = ctx.cookies()
    browser.close()

cookie_hdr = "; ".join(
    f"{c['name']}={c['value']}"
    for c in cookies
    if (c.get("domain") or "").lstrip(".").endswith("wos-giftcode.centurygame.com")
)
print("Cookie header:", "[present]" if cookie_hdr else "[none]")

safe_code = CODE[:3] + "…" if len(CODE) > 3 else CODE
ok_players = ok_redeems = fail_redeems = 0
print(f"START run — IDs={len(FIDS)}; gift_code={safe_code}")

for fid in FIDS:
    ts = str(int(time.time()))
    form_p = {"fid": fid, "time": ts}
    sign_p, _ = sign_sorted(form_p)
    form_p["sign"] = sign_p

    http_p, body_p = post_form(PLAYER_URL, form_p, cookie=None)
    try:
        js = json.loads(body_p)
    except Exception:
        js = {}
    if http_p == 200 and js.get("msg") == "success":
        data = js.get("data", {})
        nick = data.get("nickname")
        stove = data.get("stove_lv")
        print(f"PLAYER OK  fid={fid} nick={nick} stove={stove}")
        ok_players += 1
    else:
        print(f"PLAYER FAIL fid={fid} http={http_p} body={body_p[:200]}")
        continue

    ts2 = str(int(time.time()))
    form_g = {"fid": fid, "cdk": CODE, "time": ts2}
    sign_g, _ = sign_sorted(form_g)
    form_g["sign"] = sign_g

    http_g, body_g = post_form(GIFTCODE_URL, form_g, cookie_hdr)
    status = "UNKNOWN"
    try:
        rg = json.loads(body_g)
        msg = (rg.get("msg") or "").upper()
        if "SUCCESS" in msg: status = "SUCCESS"
        elif "RECEIVED" in msg: status = "ALREADY"
        elif "SAME TYPE EXCHANGE" in msg: status = "SAME_TYPE"
        elif "TIME ERROR" in msg: status = "EXPIRED"
        elif "CDK NOT FOUND" in msg: status = "INVALID"
        elif "PARAMS" in msg: status = "PARAMS_ERROR"
        else: status = msg or "UNKNOWN"
    except Exception:
        status = "PARSE_ERROR"

    print(f"GIFT fid={fid} code={safe_code} http={http_g} status={status} body={body_g[:200]}")
    if status in ("SUCCESS","ALREADY","SAME_TYPE"):
        ok_redeems += 1
    else:
        fail_redeems += 1
    time.sleep(0.3)

print(f"GIFT SUMMARY code={safe_code} success={ok_redeems} failed={fail_redeems}")
if ok_players == 0 or (ok_redeems == 0 and fail_redeems > 0):
    sys.exit(2)   # make the job red if everything failed
print("END run")
