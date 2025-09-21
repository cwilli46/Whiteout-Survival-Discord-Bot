# cogs/daily_batch.py
import os, re, json, time, asyncio, hashlib
from datetime import time as dtime, timezone
from urllib.parse import urlencode

import discord
from discord.ext import commands, tasks
import aiohttp

# ---- ENV you must set before running the bot ----
WOS_SECRET = os.getenv("WOS_SECRET", "").strip()
CODES_CH   = int(os.getenv("DISCORD_CODES_CHANNEL_ID", "0"))    # #gift_code_axe
IDS_CH     = int(os.getenv("DISCORD_IDS_CHANNEL_ID", "0"))      # #3349_axe
SUMMARY_CH = int(os.getenv("DISCORD_SUMMARY_CHANNEL_ID", "0"))  # optional; else uses CODES_CH
DAILY_UTC  = os.getenv("DAILY_RUN_UTC", "08:00")                # e.g. 08:00

if not WOS_SECRET or not CODES_CH or not IDS_CH:
    raise RuntimeError("Set WOS_SECRET, DISCORD_CODES_CHANNEL_ID, DISCORD_IDS_CHANNEL_ID env vars.")

PLAYER_URL   = "https://wos-giftcode-api.centurygame.com/api/player"
GIFTCODE_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
ORIGIN       = "https://wos-giftcode.centurygame.com"
REFERER      = "https://wos-giftcode.centurygame.com/"

YAML_CODES = re.compile(r"(?mi)^\s*codes\s*:\s*$")
YAML_FIDS  = re.compile(r"(?mi)^\s*fids\s*:\s*$")
BULLET     = re.compile(r"(?m)^\s*-\s*(\S+)\s*$")
CSV_CODE   = re.compile(r"(?mi)^\s*([A-Za-z0-9]{4,24})\s*$")
CSV_FID    = re.compile(r"(?mi)^\s*(\d{6,12})\s*$")

def md5(s: str) -> str:
    import hashlib as _h
    return _h.md5(s.encode("utf-8")).hexdigest()

def sign_sorted(form: dict, secret: str) -> str:
    items = sorted((k, str(v)) for k, v in form.items())
    base  = "&".join([f"{k}={v}" for k, v in items])
    return md5(base + secret)

class DailyBatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # small state kept in settings.sqlite -> table daily_state(key TEXT PRIMARY KEY, value TEXT)
        self.state_conn = getattr(bot, "settings_conn", None)
        if self.state_conn is None:
            import sqlite3
            os.makedirs("db", exist_ok=True)
            self.state_conn = sqlite3.connect("db/settings.sqlite")
        self.cur = self.state_conn.cursor()
        self.cur.execute("CREATE TABLE IF NOT EXISTS daily_state (key TEXT PRIMARY KEY, value TEXT)")
        self.cur.execute("CREATE TABLE IF NOT EXISTS roster (fid TEXT PRIMARY KEY, nickname TEXT, stove INTEGER, updated_at INTEGER)")
        self.state_conn.commit()

        hh, mm = map(int, DAILY_UTC.split(":"))
        self.run_time = dtime(hour=hh, minute=mm, tzinfo=timezone.utc)
        self.daily_loop.change_interval(time=self.run_time)  # set clock before start
        self.daily_loop.start()

    def cog_unload(self):
        self.daily_loop.cancel()
        try: self.state_conn.close()
        except: pass

    # ---------- helpers ----------
    def _get_state(self, key: str):
        self.cur.execute("SELECT value FROM daily_state WHERE key=?", (key,))
        row = self.cur.fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value: str):
        self.cur.execute("INSERT OR REPLACE INTO daily_state(key,value) VALUES(?,?)", (key, value))
        self.state_conn.commit()

    async def _fetch_text_attachments(self, msg: discord.Message):
        texts = []
        for att in msg.attachments:
            name = att.filename.lower()
            if name.endswith((".txt", ".csv", ".yml", ".yaml")):
                texts.append(await att.read())
        return [t.decode("utf-8", "ignore") for t in texts]

    def _parse_codes(self, text: str):
        out, current = set(), None
        for line in text.splitlines():
            if YAML_CODES.match(line): current="codes"; continue
            m = BULLET.match(line)
            if m and current=="codes": out.add(m.group(1).strip().upper()); continue
            m2 = CSV_CODE.match(line)
            if m2: out.add(m2.group(1).upper())
        return out

    def _parse_fids(self, text: str):
        out, current = set(), None
        for line in text.splitlines():
            if YAML_FIDS.match(line): current="fids"; continue
            m = BULLET.match(line)
            if m and current=="fids": out.add(m.group(1).strip()); continue
            m2 = CSV_FID.match(line)
            if m2: out.add(m2.group(1))
        return out

    async def _get_cookie_header(self):
        """
        Grab first-party cookie using Playwright so the API accepts our POSTs.
        Requires: playwright installed; run once: `python -m playwright install`
        """
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(REFERER, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(2000)
            cookies = await ctx.cookies()
            await browser.close()
        pairs = []
        for c in cookies:
            domain = (c.get("domain") or "").lstrip(".")
            if domain.endswith("wos-giftcode.centurygame.com"):
                pairs.append(f"{c['name']}={c['value']}")
        return "; ".join(pairs)

    async def _post_form(self, session: aiohttp.ClientSession, url: str, form: dict, cookie: str | None):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": ORIGIN, "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        }
        if cookie: headers["Cookie"] = cookie
        data = urlencode(form)
        async with session.post(url, headers=headers, data=data, timeout=30) as r:
            text = await r.text()
            return r.status, text

    # ---------- the daily job ----------
    @tasks.loop(time=dtime(hour=8, tzinfo=timezone.utc))
    async def daily_loop(self):
        await self.bot.wait_until_ready()
        codes_ch = self.bot.get_channel(CODES_CH) or await self.bot.fetch_channel(CODES_CH)
        ids_ch   = self.bot.get_channel(IDS_CH)   or await self.bot.fetch_channel(IDS_CH)
        summary  = self.bot.get_channel(SUMMARY_CH or CODES_CH) or await self.bot.fetch_channel(SUMMARY_CH or CODES_CH)

        # read new codes since last run
        last_codes = int(self._get_state("last_id_codes") or 0)
        last_ids   = int(self._get_state("last_id_ids") or 0)

        codes, new_fids = set(), set()

        async for m in codes_ch.history(limit=200, after=discord.Object(id=last_codes) if last_codes else None, oldest_first=True):
            codes |= self._parse_codes(m.content or "")
            for t in await self._fetch_text_attachments(m):
                codes |= self._parse_codes(t)
            last_codes = max(last_codes, int(m.id))
        async for m in ids_ch.history(limit=200, after=discord.Object(id=last_ids) if last_ids else None, oldest_first=True):
            new_fids |= self._parse_fids(m.content or "")
            for t in await self._fetch_text_attachments(m):
                new_fids |= self._parse_fids(t)
            last_ids = max(last_ids, int(m.id))

        # checkpoint
        self._set_state("last_id_codes", str(last_codes))
        self._set_state("last_id_ids",   str(last_ids))

        # update roster
        added = []
        for fid in sorted(new_fids):
            self.cur.execute("INSERT OR IGNORE INTO roster(fid) VALUES(?)", (fid,))
            if self.cur.rowcount: added.append(fid)
        self.state_conn.commit()

        if not codes and not added:
            await summary.send("ℹ️ Daily batch: no new codes or IDs today.")
            return

        # cookie + session
        cookie = await self._get_cookie_header()
        ok_players = 0
        furnace_ups = []
        redeem_lines = []
        ok_redeems = fail_redeems = 0

        async with aiohttp.ClientSession() as sess:
            # scan furnace for all roster
            self.cur.execute("SELECT fid, nickname, stove FROM roster")
            roster = self.cur.fetchall()  # list of (fid, nick, stove)
            for fid, prev_nick, prev_stove in roster:
                ts = str(int(time.time()))
                payload = {"fid": fid, "time": ts}
                payload["sign"] = sign_sorted(payload, WOS_SECRET)
                hp, bp = await self._post_form(sess, PLAYER_URL, payload, cookie=None)
                try:
                    js = json.loads(bp)
                except Exception:
                    js = {}
                if hp == 200 and js.get("msg") == "success":
                    ok_players += 1
                    data = js.get("data", {})
                    nick  = data.get("nickname")
                    stove = data.get("stove_lv")
                    # update
                    self.cur.execute(
                        "UPDATE roster SET nickname=?, stove=?, updated_at=? WHERE fid=?",
                        (nick, int(stove) if stove is not None else None, int(time.time()), fid)
                    )
                    # furnace diff
                    try:
                        if prev_stove is not None and stove is not None and int(stove) > int(prev_stove):
                            furnace_ups.append(f"🔥 `{fid}` {nick or ''} • {prev_stove} ➜ {stove}")
                    except Exception:
                        pass
                await asyncio.sleep(0.15)
            self.state_conn.commit()

            # redeem: new codes for ALL roster FIDs
            self.cur.execute("SELECT fid FROM roster")
            all_fids = [r[0] for r in self.cur.fetchall()]
            for code in sorted(codes):
                for fid in sorted(all_fids):
                    ts2 = str(int(time.time()))
                    pay = {"fid": fid, "cdk": code, "time": ts2}
                    pay["sign"] = sign_sorted(pay, WOS_SECRET)
                    hg, bg = await self._post_form(sess, GIFTCODE_URL, pay, cookie)
                    status = "UNKNOWN"
                    try:
                        rg = json.loads(bg); msg = (rg.get("msg") or "").upper()
                        if "SUCCESS" in msg: status = "SUCCESS"
                        elif "RECEIVED" in msg: status = "ALREADY"
                        elif "SAME TYPE EXCHANGE" in msg: status = "SAME_TYPE"
                        elif "TIME ERROR" in msg: status = "EXPIRED"
                        elif "CDK NOT FOUND" in msg: status = "INVALID"
                        elif "PARAMS" in msg: status = "PARAMS_ERROR"
                        else: status = msg or "UNKNOWN"
                    except Exception:
                        status = "PARSE_ERROR"
                    ok = status in ("SUCCESS","ALREADY","SAME_TYPE")
                    ok_redeems += int(ok); fail_redeems += int(not ok)
                    emoji = "✅" if ok else "❌"
                    redeem_lines.append(f"{emoji} {fid} • {code} • {status}")
                    await asyncio.sleep(0.15)

        # summary
        parts = []
        if added:        parts.append("**New IDs**\n" + ", ".join(f"`{a}`" for a in added[:20]) + (" …" if len(added)>20 else ""))
        if codes:        parts.append("**Codes**\n" + ", ".join(f"`{c}`" for c in sorted(codes)))
        if furnace_ups:  parts.append("**Furnace level ups**\n" + "\n".join(furnace_ups[:15]) + (" \n…" if len(furnace_ups)>15 else ""))
        if redeem_lines: parts.append("**Redeem results (first 25)**\n" + "\n".join(redeem_lines[:25]) + (" \n…" if len(redeem_lines)>25 else ""))

        summary_text = (
            f"**Daily Batch Summary**\n"
            f"Players checked: {ok_players}\n"
            f"Redeems: {ok_redeems} ok / {fail_redeems} failed\n"
            + ("\n\n".join(parts) if parts else "\n(No changes)")
        )
        await summary.send(summary_text)

    @daily_loop.before_loop
    async def before(self):
        await self.bot.wait_until_ready()
        # make sure we have permissions on channels
        for cid in (CODES_CH, IDS_CH, SUMMARY_CH or CODES_CH):
            ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            _ = ch.name  # force fetch / raise early if bad id


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyBatch(bot))
