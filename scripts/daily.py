import os, re, json, time, hashlib, requests, pathlib, datetime
from typing import List, Dict, Tuple

# was: parents[2]
ROOT = pathlib.Path(__file__).resolve().parents[1]  # from /scripts to repo root
STATE = ROOT / "state" / "members.json"
LOGS  = ROOT / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

# ---- Config from env
ALLIANCE = os.getenv("ALLIANCE_NAME", "Alliance")
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CHAN_FIDS = os.getenv("DISCORD_IDS_CHANNEL_ID", "")
CHAN_CODES = os.getenv("DISCORD_CODES_CHANNEL_ID", "")
CHAN_SUMMARY = os.getenv("DISCORD_SUMMARY_CHANNEL_ID", "")
WOS_SECRET = os.getenv("WOS_SECRET", "tB87#kPtkxqOS2").strip()
PACE = float(os.getenv("REDEEM_PACING_SECONDS", "0.3"))

# ---- WOS endpoints
WOS_PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
WOS_ORIGIN       = "https://wos-giftcode.centurygame.com"

# ---- Helpers
def log(msg, fname="daily.log"):
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with open(LOGS / fname, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def md5sign(data: dict) -> dict:
    parts = []
    for k in sorted(data.keys()):
        v = data[k]
        if isinstance(v, dict):
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        parts.append(f"{k}={v}")
    encoded = "&".join(parts)
    sign = hashlib.md5((encoded + WOS_SECRET).encode("utf-8")).hexdigest()
    return {"sign": sign, **data}

def wos_session():
    s = requests.Session()
    s.headers.update({
        "accept": "application/json, text/plain, */*",
        "content-type": "application/x-www-form-urlencoded",
        "origin": WOS_ORIGIN,
        "referer": WOS_ORIGIN + "/",
        "user-agent": "Mozilla/5.0 GitHubActions WOSBot",
    })
    return s

def discord_session():
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json"
    })
    return s

def discord_fetch_all_messages(channel_id: str, limit_total: int = 1000) -> List[dict]:
    """Page through recent messages so you can store FIDs/Codes in pinned or recent posts."""
    s = discord_session()
    msgs, last_id = [], None
    remaining = limit_total
    while remaining > 0:
        params = {"limit": min(100, remaining)}
        if last_id:
            params["before"] = last_id
        r = s.get(f"https://discord.com/api/v10/channels/{channel_id}/messages", params=params, timeout=30)
        if r.status_code != 200:
            log(f"Discord GET messages {channel_id} -> {r.status_code} {r.text}", "discord.log")
            break
        batch = r.json()
        if not batch:
            break
        msgs.extend(batch)
        last_id = batch[-1]["id"]
        remaining -= len(batch)
    log(f"Fetched {len(msgs)} msgs from {channel_id}", "discord.log")
    return msgs

def discord_post(channel_id: str, content: str):
    if not channel_id or not content:
        return
    s = discord_session()
    # chunk to 2000-char Discord limit
    chunks = []
    while content:
        chunks.append(content[:1900])
        content = content[1900:]
    for c in chunks:
        r = s.post(f"https://discord.com/api/v10/channels/{channel_id}/messages",
                   json={"content": c}, timeout=30)
        log(f"POST to {channel_id} -> {r.status_code}", "discord.log")
        time.sleep(1)

# ---- Parsers
FID_RE = re.compile(r"\b\d{3,18}\b")  # FID = numeric id, fairly long
CODE_RE = re.compile(r"\b[A-Z0-9]{4,20}\b", re.I)

def parse_fids_from_messages(msgs: List[dict]) -> List[str]:
    fids = set()
    for m in msgs:
        if m.get("author", {}).get("bot"):
            # allow bot posts too—teams sometimes paste lists via bot
            pass
        text = (m.get("content") or "").strip()
        # Skip obvious comments
        if text.startswith("#"):
            continue
        for tok in FID_RE.findall(text):
            fids.add(tok)
    return sorted(fids, key=lambda x: int(x))

def parse_codes_from_messages(msgs: List[dict]) -> List[str]:
    codes = []
    seen = set()
    for m in msgs:
        text = (m.get("content") or "").upper()
        if text.startswith("#"):
            continue
        for tok in CODE_RE.findall(text):
            if tok not in seen:
                seen.add(tok)
                codes.append(tok)
    return codes

# ---- State
def load_snapshot() -> Dict[str, dict]:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}

def save_snapshot(snapshot: Dict[str, dict]):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

# ---- WOS
def fetch_player(fid: str):
    payload = {"fid": fid, "time": int(time.time())}
    data = md5sign(payload)
    r = wos_session().post(WOS_PLAYER_URL, data=data, timeout=30)
    try:
        j = r.json()
    except Exception:
        j = {"status": r.status_code, "text": r.text[:200]}
    log(f"player {fid} -> {j}", "wos_api.log")
    if isinstance(j, dict) and j.get("msg") == "success" and "data" in j:
        d = j["data"]
        return {
            "fid": str(d.get("fid", fid)),
            "nickname": (d.get("nickname") or "").strip(),
            "furnace_lv": int(d.get("furnace_lv", 0)),
        }
    return None

def summarize_diffs(prev: Dict[str, dict], curr: Dict[str, dict]) -> Tuple[str, str]:
    name_changes, furnace_changes = [], []
    for fid, now in curr.items():
        before = prev.get(fid)
        if not before:
            continue
        if now["nickname"] and before.get("nickname") and now["nickname"] != before["nickname"]:
            name_changes.append((before["nickname"], now["nickname"], fid))
        if int(now["furnace_lv"]) != int(before.get("furnace_lv", 0)):
            furnace_changes.append((before.get("furnace_lv", 0), now["furnace_lv"], now["nickname"], fid))

    if name_changes:
        lines = [f"📝 **Recent Nickname Changes — {ALLIANCE}**"]
        for old, new, fid in name_changes[:50]:
            lines.append(f"- `{old}` → `{new}` (FID {fid})")
        name_msg = "\n".join(lines)
    else:
        name_msg = f"📝 **Recent Nickname Changes — {ALLIANCE}**\n- None in this window."

    if furnace_changes:
        lines = [f"🔥 **Recent Furnace Changes — {ALLIANCE}**"]
        for old, new, nick, fid in sorted(furnace_changes, key=lambda x: (x[1], x[2]))[:50]:
            label = nick if nick else f"FID {fid}"
            lines.append(f"- `{label}`: **{old} → {new}** (FID {fid})")
        furnace_msg = "\n".join(lines)
    else:
        furnace_msg = f"🔥 **Recent Furnace Changes — {ALLIANCE}**\n- None in this window."

    return name_msg, furnace_msg

def codes_summary(codes: List[str]) -> str:
    if not codes:
        return ""
    lines = [f"🎁 **Gift Codes (manual redeem)**"]
    for c in codes[:50]:
        lines.append(f"- `{c}`  → https://wos-giftcode.centurygame.com/")
    if len(codes) > 50:
        lines.append(f"...and {len(codes)-50} more.")
    return "\n".join(lines)

# ---- Main
def main():
    if not BOT_TOKEN:
        log("Missing DISCORD_BOT_TOKEN; abort")
        return
    if not CHAN_FIDS:
        log("Missing DISCORD_IDS_CHANNEL_ID; abort")
        return
    if not CHAN_SUMMARY:
        log("Missing DISCORD_SUMMARY_CHANNEL_ID; abort")
        return

    # 1) Pull FIDs and Codes from Discord channels
    fid_msgs = discord_fetch_all_messages(CHAN_FIDS, limit_total=400)
    fids = parse_fids_from_messages(fid_msgs)
    if not fids:
        discord_post(CHAN_SUMMARY, "⚠️ No FIDs found in the IDs channel. Please paste numeric FIDs (one per line).")
        log("No FIDs parsed; abort")
        return

    code_list = []
    if CHAN_CODES:
        code_msgs = discord_fetch_all_messages(CHAN_CODES, limit_total=400)
        code_list = parse_codes_from_messages(code_msgs)

    # 2) Fetch current player data
    prev = load_snapshot()
    curr = {}
    for fid in fids:
        data = fetch_player(fid)
        if data:
            curr[fid] = data
        time.sleep(PACE)

    # 3) Diff and post summary
    name_msg, furnace_msg = summarize_diffs(prev, curr)
    summary = f"**Daily WOS Summary — {ALLIANCE}**\n" \
              f"{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n" \
              f"{name_msg}\n\n{furnace_msg}"

    discord_post(CHAN_SUMMARY, summary)

    # 4) Codes (broadcast as manual redeem list — no auto-redeem to avoid CAPTCHA/TOS issues)
    if code_list:
        discord_post(CHAN_SUMMARY, codes_summary(code_list))

    # 5) Save snapshot for tomorrow
    save_snapshot(curr)

if __name__ == "__main__":
    main()
