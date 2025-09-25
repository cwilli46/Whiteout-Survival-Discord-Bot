import os, json, time, hashlib, requests, pathlib, datetime
from urllib.parse import urlencode

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / "state" / "members.json"
LOGS  = ROOT / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

WOS_PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
WOS_GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
WOS_ORIGIN       = "https://wos-giftcode.centurygame.com"
SECRET           = "tB87#kPtkxqOS2"  # matches the repo’s cogs

def log(msg, fname="daily.log"):
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with open(LOGS / fname, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def md5sign(data: dict) -> dict:
    # API expects sign over keys sorted lexicographically, appended with SECRET
    parts = []
    for k in sorted(data.keys()):
        v = data[k]
        if isinstance(v, dict):
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        parts.append(f"{k}={v}")
    encoded = "&".join(parts)
    sign = hashlib.md5((encoded + SECRET).encode("utf-8")).hexdigest()
    return {"sign": sign, **data}

def session():
    s = requests.Session()
    s.headers.update({
        "accept": "application/json, text/plain, */*",
        "content-type": "application/x-www-form-urlencoded",
        "origin": WOS_ORIGIN,
        "referer": WOS_ORIGIN + "/",
        "user-agent": "Mozilla/5.0 GitHubActions WOSBot",
    })
    s.timeout = 30
    return s

def fetch_player(fid: str):
    payload = {"fid": fid, "time": int(time.time())}
    data = md5sign(payload)
    r = session().post(WOS_PLAYER_URL, data=data)
    j = r.json()
    log(f"player {fid} -> {j}", "wos_api.log")
    # Expect {"msg":"success","data":{"fid":...,"nickname":...,"furnace_lv":...}}
    if j.get("msg") == "success" and "data" in j:
        d = j["data"]
        return {
            "fid": str(d.get("fid", fid)),
            "nickname": d.get("nickname", "").strip(),
            "furnace_lv": int(d.get("furnace_lv", 0)),
        }
    return None

def load_snapshot():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}

def save_snapshot(snapshot: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

def webhook(url: str, content: str):
    if not url:
        log("No webhook URL provided; skipping post")
        return
    resp = requests.post(url, json={"content": content[:1990]})
    log(f"webhook status {resp.status_code}")

def summarize_diffs(prev: dict, curr: dict, alliance: str):
    name_changes = []
    furnace_changes = []
    for fid, now in curr.items():
        before = prev.get(fid)
        if not before:
            continue
        if now["nickname"] and before.get("nickname") and now["nickname"] != before["nickname"]:
            name_changes.append((before["nickname"], now["nickname"], fid))
        if int(now["furnace_lv"]) != int(before.get("furnace_lv", 0)):
            furnace_changes.append((before.get("furnace_lv", 0), now["furnace_lv"], now["nickname"], fid))

    name_msg = ""
    if name_changes:
        lines = [f"📝 **Recent Nickname Changes — {alliance}**"]
        for old, new, fid in name_changes[:40]:
            lines.append(f"- `{old}` → `{new}` (FID {fid})")
        name_msg = "\n".join(lines)
    else:
        name_msg = f"📝 **Recent Nickname Changes — {alliance}**\n- None in this window."

    furnace_msg = ""
    if furnace_changes:
        lines = [f"🔥 **Recent Furnace Changes — {alliance}**"]
        for old, new, nick, fid in sorted(furnace_changes, key=lambda x: x[1])[:40]:
            lines.append(f"- `{nick}` (FID {fid}): **{old} → {new}**")
        furnace_msg = "\n".join(lines)
    else:
        furnace_msg = f"🔥 **Recent Furnace Changes — {alliance}**\n- None in this window."

    return name_msg, furnace_msg

def code_link(code: str, fid: str) -> str:
    # Broadcast a prefilled redemption URL to keep this fully manual & TOS-safe.
    # (Do not auto-post the sign/time; the website will drive the flow.)
    return f"https://wos-giftcode.centurygame.com/?fid={fid}&cdk={code}"

def broadcast_codes(alliance: str, test_fid: str):
    path = ROOT / "giftcodes.txt"
    if not path.exists():
        log("No giftcodes.txt found; skip code broadcast")
        return None

    codes = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        c = raw.strip().split()[0]
        if c and c.isalnum():
            codes.append(c)

    if not codes:
        return None

    lines = [f"🎁 **Gift Codes for {alliance}**"]
    sample = codes[:20]
    for c in sample:
        if test_fid:
            lines.append(f"- `{c}` → {code_link(c, test_fid)}")
        else:
            lines.append(f"- `{c}` (open the redemption site and paste)")

    if len(codes) > len(sample):
        lines.append(f"...and {len(codes)-len(sample)} more.")

    return "\n".join(lines)

def main():
    alliance = os.getenv("ALLIANCE_NAME", "Alliance")
    fids = [f.strip() for f in os.getenv("WOS_MEMBER_FIDS", "").split(",") if f.strip()]
    if not fids:
        log("WOS_MEMBER_FIDS empty; nothing to do")
        return

    prev = load_snapshot()
    curr = {}

    for fid in fids:
        data = fetch_player(fid)
        if data:
            curr[fid] = data
        time.sleep(0.3)  # be nice

    name_msg, furnace_msg = summarize_diffs(prev, curr, alliance)
    save_snapshot(curr)

    webhook(os.getenv("DISCORD_WEBHOOK_CHANGES", ""), name_msg)
    webhook(os.getenv("DISCORD_WEBHOOK_FURNACE", ""), furnace_msg)

    codes_msg = broadcast_codes(alliance, os.getenv("TEST_FID", "").strip())
    if codes_msg:
        webhook(os.getenv("DISCORD_WEBHOOK_CODES", ""), codes_msg)

if __name__ == "__main__":
    main()
