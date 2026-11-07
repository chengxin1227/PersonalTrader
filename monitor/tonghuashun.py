from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET

import httpx
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from monitor.config import Settings

logger = logging.getLogger(__name__)

_AUTH_URL = "https://auth.10jqka.com.cn/verify2"
_COOKIE_URL = "https://upass.10jqka.com.cn/docookie2.php"
_QUERY_URL = "https://ugc.10jqka.com.cn/optdata/selfstock/open/api/v1/query"
_MODIFY_URL = "https://t.10jqka.com.cn/newcircle/group/modifySelfStock/"
_GROUP_URL = "https://ugc.10jqka.com.cn/optdata/selfgroup/open/api"
_LOGIN_AGENT = "同花顺/7.0.10 CFNetwork/1333.0.4 Darwin/21.5.0".encode("utf-8")
_API_AGENT = "hevo"
_GROUP_AGENT = "Hexin_Gphone/11.28.03 (Royal Flush) okhttp/3.14.9"
_SESSION_SECONDS = 12 * 60 * 60
# Custom groups (not the default 自选股 list).
_DAILY_WATCHLIST_GROUP = "今日自选"  # every alerted name
_DEFAULT_IMPORTANT_GROUP = "我的自选"  # important subset only
# When a session opens, summary groups drop names tagged with the previous session.
_PREVIOUS_SESSION = {"盘前": "盘后", "盘中": "盘前", "盘后": "盘中"}


class TonghuashunError(RuntimeError):
    pass


def parse_selfstock(raw: str) -> list[tuple[str, str]]:
    """'AAPL|600519,185|17' -> [('AAPL', '185'), ('600519', '17')]."""
    if not raw:
        return []
    comma = raw.rfind(",")
    if comma < 0:
        return []
    codes = [code for code in raw[:comma].split("|") if code]
    markets = [market for market in raw[comma + 1 :].split("|") if market]
    return [(code, markets[index] if index < len(markets) else "") for index, code in enumerate(codes)]


def format_selfstock(rows: list[tuple[str, str]]) -> str:
    return "|".join(code for code, _ in rows) + "," + "|".join(market for _, market in rows)


def _xml_item(text: str, action: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TonghuashunError(f"同花顺{action}响应无法解析") from exc
    ret = root.find("ret")
    if ret is None or str(ret.attrib.get("code")) != "0":
        message = ret.attrib.get("msg") if ret is not None else ""
        raise TonghuashunError(f"同花顺{action}失败: {message or '未知错误'}")
    item = root.find("item")
    if item is None:
        raise TonghuashunError(f"同花顺{action}响应缺少内容")
    return item


def _encrypt(pubkey: str, value: str) -> str:
    # 同花顺 sends the PEM on one line, with spaces where the line breaks belong.
    body = pubkey.replace("-----BEGIN PUBLIC KEY-----", "").replace("-----END PUBLIC KEY-----", "")
    cipher = PKCS1_v1_5.new(RSA.import_key(base64.b64decode("".join(body.split()))))
    return base64.b64encode(cipher.encrypt(value.encode("utf-8"))).decode("ascii")


class TonghuashunClient:
    """Adds alerted names to a rule group, 今日自选, and optionally 我的自选."""

    def __init__(self, settings: Settings) -> None:
        self._username = settings.ths_username
        self._password = settings.ths_password
        self._session_path = settings.data_dir / "ths_session.json"
        self._markets_path = settings.data_dir / "ths_markets.json"
        self._cookies: dict[str, str] | None = None
        self._saved_at = 0.0
        self._days_path = settings.data_dir / "ths_group_days.json"
        self._watchlist_sessions_path = settings.data_dir / "ths_watchlist_sessions.json"
        self._important_sessions_path = settings.data_dir / "ths_important_watchlist_sessions.json"
        self._lock = threading.Lock()
        self._markets: dict[str, str] | None = None
        self._days: dict[str, str] | None = None
        self._watchlist_sessions: dict[str, str] | None = None
        self._important_sessions: dict[str, str] | None = None
        self._important_group = _DEFAULT_IMPORTANT_GROUP

    @property
    def configured(self) -> bool:
        return bool(self._username and self._password)

    def set_important_group(self, name: str) -> None:
        cleaned = (name or "").strip() or _DEFAULT_IMPORTANT_GROUP
        self._important_group = cleaned

    @property
    def important_group(self) -> str:
        return self._important_group

    def _load_session(self) -> None:
        if self._cookies is not None or not self._session_path.exists():
            return
        try:
            payload = json.loads(self._session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cookies = payload.get("cookies")
        saved_at = float(payload.get("saved_at") or 0)
        if isinstance(cookies, dict) and cookies and time.time() - saved_at < _SESSION_SECONDS:
            self._cookies = {str(key): str(value) for key, value in cookies.items()}
            self._saved_at = saved_at

    def _save_session(self) -> None:
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_path.write_text(
            json.dumps({"cookies": self._cookies, "saved_at": self._saved_at}), encoding="utf-8"
        )
        self._session_path.chmod(0o600)

    def _login(self, http: httpx.Client) -> None:
        headers = {"User-Agent": _LOGIN_AGENT}
        key = _xml_item(
            http.get(_AUTH_URL, params={"reqtype": "do_rsa", "type": "get_pubkey"}, headers=headers).text,
            "取登录密钥",
        )
        pubkey = key.attrib.get("pubkey") or ""
        rsa_version = key.attrib.get("rsa_version") or "default_5"
        if not pubkey:
            raise TonghuashunError("同花顺取登录密钥失败")
        login = _xml_item(
            http.get(
                _AUTH_URL,
                params={
                    "account": _encrypt(pubkey, self._username),
                    "passwd": _encrypt(pubkey, self._password),
                    "msg": "1",
                    "reqtype": "unified_login",
                    "rsa_version": rsa_version,
                    "ta_appid": "2022021114090152",
                },
                headers=headers,
            ).text,
            "登录",
        )
        userid = login.attrib.get("userid") or ""
        sessionid = login.attrib.get("sessionid") or ""
        if not userid or not sessionid:
            raise TonghuashunError("同花顺登录没有返回会话")
        verify = _xml_item(
            http.get(
                _AUTH_URL,
                params={
                    "reqtype": "mainverify",
                    "userid": userid,
                    "sessionid": sessionid,
                    "qsid": "8003",
                    "product": "S01",
                    "version": "11.4.1.3",
                    "imei": "ZjI6MDY6NGE6NzI6MjQ6NTA=",
                    "sdsn": "",
                    "rsa_version": login.attrib.get("rsa_version") or rsa_version,
                    "nohqlist": "0",
                    "securities": "%E5%90%8C%E8%8A%B1%E9%A1%BA%E8%BF%9C%E8%88%AA%E7%89%88",
                },
                headers=headers,
            ).text,
            "验证",
        )
        passport = dict(
            chunk.split("=", 1) for chunk in (verify.attrib.get("passport") or "").split("|") if "=" in chunk
        )
        signvalid = passport.get("signvalid", "").strip()
        if not signvalid:
            raise TonghuashunError("同花顺验证没有返回签名")
        response = http.get(
            _COOKIE_URL,
            params={"userid": userid, "sessionid": sessionid, "signvalid": signvalid},
            headers=headers,
        )
        response.raise_for_status()
        cookies = dict(response.cookies.items())
        if not cookies:
            raise TonghuashunError("同花顺登录没有返回 Cookie")
        cookies.setdefault("userid", userid)
        self._cookies = cookies
        self._saved_at = time.time()
        self._save_session()
        logger.info("同花顺 logged in")

    def _api_headers(self) -> dict[str, str]:
        headers = {"User-Agent": _API_AGENT}
        userid = (self._cookies or {}).get("userid")
        if userid:
            headers["userid"] = userid
        return headers

    def _query(self, http: httpx.Client) -> tuple[str, list[tuple[str, str]]]:
        response = http.get(
            _QUERY_URL,
            params={"support_all": "0", "from": "thspc_hevo"},
            headers=self._api_headers(),
            cookies=self._cookies,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status_code") != 0:
            raise TonghuashunError(f"同花顺读自选失败: {payload.get('status_msg') or '未知错误'}")
        data = payload.get("data") or {}
        return str(data.get("version", "")), parse_selfstock(str(data.get("selfstock") or ""))

    def _modify(self, http: httpx.Client, op: str, symbol: str) -> None:
        # 同花顺 resolves the market from the code, e.g. KO lands on NYSE (169).
        response = http.get(
            _MODIFY_URL,
            params={"op": op, "stockcode": symbol},
            headers={"User-Agent": _API_AGENT},
            cookies=self._cookies,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errorCode") != 0:
            raise TonghuashunError(f"同花顺加自选失败: {payload.get('errorMsg') or '未知错误'}")

    def _with_session(self, work):
        with self._lock, httpx.Client(timeout=10, follow_redirects=True) as http:
            self._load_session()
            if self._cookies is None:
                self._login(http)
            try:
                return work(http)
            except (TonghuashunError, httpx.HTTPStatusError):
                # Expired sessions fail as a business error; log in again once.
                self._cookies = None
                self._login(http)
                return work(http)

    def _known_markets(self) -> dict[str, str]:
        if self._markets is None:
            try:
                self._markets = json.loads(self._markets_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._markets = {}
        return self._markets

    def _market(self, http: httpx.Client, symbol: str) -> str:
        """同花顺 market id. Group writes need it, and it does not follow the exchange."""
        known = self._known_markets()
        if symbol in known:
            return known[symbol]
        _version, items = self._query(http)
        market = next((m for code, m in items if code.upper() == symbol), "")
        if not market:
            # Only the default 自选股 list resolves a bare code. Add, read id, then remove.
            self._modify(http, "add", symbol)
            _version, items = self._query(http)
            market = next((m for code, m in items if code.upper() == symbol), "")
            self._modify(http, "del", symbol)
        if not market:
            raise TonghuashunError(f"同花顺找不到 {symbol}")
        known[symbol] = market
        self._markets_path.write_text(json.dumps(known, indent=2, sort_keys=True), encoding="utf-8")
        return market

    def _session_tags(self, *, important: bool) -> dict[str, str]:
        if important:
            if self._important_sessions is None:
                self._important_sessions = self._load_session_tags(self._important_sessions_path)
            return self._important_sessions
        if self._watchlist_sessions is None:
            self._watchlist_sessions = self._load_session_tags(self._watchlist_sessions_path)
        return self._watchlist_sessions

    def _load_session_tags(self, path) -> dict[str, str]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {
                str(symbol).upper(): str(session)
                for symbol, session in raw.items()
                if symbol and session
            }
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_session_tags(self, *, important: bool) -> None:
        tags = self._session_tags(important=important)
        path = self._important_sessions_path if important else self._watchlist_sessions_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(tags, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _ensure_summary_group(
        self,
        http: httpx.Client,
        group_name: str,
        symbols: list[str],
        session: str,
        *,
        important: bool,
    ) -> None:
        """Keep alerted names in a custom summary group (今日自选 / 我的自选)."""
        if not symbols:
            return
        group_id, rows = self._group(http, group_name)
        known = self._known_markets()
        for code, market in rows:
            known.setdefault(code.upper(), market)
        existing = [code.upper() for code, _ in rows]
        fresh = [symbol for symbol in symbols if symbol not in existing]
        tags = self._session_tags(important=important)
        for symbol in symbols:
            tags[symbol] = session
        self._save_session_tags(important=important)
        if not fresh:
            return
        for symbol in fresh:
            self._market(http, symbol)
        ordered = fresh + existing
        self._write(http, "/content/v1/delete", group_id, rows)
        self._write(http, "/content/v1/add", group_id, [(code, known[code]) for code in ordered])
        logger.info("同花顺 %s: %s", group_name, ", ".join(fresh))

    def _clear_summary_session_sync(self, session: str, group_name: str, *, important: bool) -> list[str]:
        """Remove summary-group names that were added in ``session`` (previous period)."""

        def work(http: httpx.Client) -> list[str]:
            group_id, rows = self._group(http, group_name)
            if not rows:
                tags = self._session_tags(important=important)
                stale = [symbol for symbol, tagged in list(tags.items()) if tagged == session]
                for symbol in stale:
                    del tags[symbol]
                if stale:
                    self._save_session_tags(important=important)
                return []
            tags = self._session_tags(important=important)
            known = self._known_markets()
            keep_rows: list[tuple[str, str]] = []
            removed: list[str] = []
            for code, market in rows:
                symbol = code.upper()
                known.setdefault(symbol, market)
                tagged = tags.get(symbol)
                # Untagged leftovers count as the session being cleared once.
                if tagged == session or tagged is None:
                    removed.append(symbol)
                    tags.pop(symbol, None)
                else:
                    keep_rows.append((symbol, known[symbol]))
            if not removed:
                return []
            self._write(http, "/content/v1/delete", group_id, rows)
            if keep_rows:
                self._write(http, "/content/v1/add", group_id, keep_rows)
            self._save_session_tags(important=important)
            logger.info("同花顺 %s cleared %s: %s", group_name, session, ", ".join(removed))
            return removed

        return self._with_session(work)

    def _group_call(self, http: httpx.Client, method: str, path: str, data: dict[str, str]) -> dict:
        headers = {"User-Agent": _GROUP_AGENT}
        if method == "GET":
            response = http.get(_GROUP_URL + path, params=data, headers=headers, cookies=self._cookies)
        else:
            response = http.post(_GROUP_URL + path, data=data, headers=headers, cookies=self._cookies)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status_code") != 0:
            raise TonghuashunError(f"同花顺分组操作失败: {payload.get('status_msg') or '未知错误'}")
        return payload.get("data") or {}

    def _groups(self, http: httpx.Client) -> tuple[str, list[dict]]:
        data = self._group_call(
            http, "GET", "/group/v1/query", {"from": "sjcg_gphone", "types": "0,1"}
        )
        return str(data.get("version", "")), list(data.get("group_list") or [])

    def _group_days(self) -> dict[str, str]:
        if self._days is None:
            try:
                self._days = json.loads(self._days_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._days = {}
        return self._days

    def _save_group_days(self) -> None:
        days = self._group_days()
        self._days_path.parent.mkdir(parents=True, exist_ok=True)
        self._days_path.write_text(json.dumps(days, ensure_ascii=False, indent=2), encoding="utf-8")

    def _custom_groups(self, http: httpx.Client) -> list[dict]:
        _version, groups = self._groups(http)
        return [g for g in groups if str(g.get("id", "")).startswith("0_")]

    def _clear_group_rows(
        self, http: httpx.Client, name: str, group_id: str, rows: list[tuple[str, str]], day: str
    ) -> None:
        self._write(http, "/content/v1/delete", group_id, rows)
        if rows:
            logger.info("同花顺 cleared %s from an earlier day", name)
        days = self._group_days()
        days[name] = day
        self._save_group_days()

    def _group(self, http: httpx.Client, name: str) -> tuple[str, list[tuple[str, str]]]:
        """Group id and its (code, market) rows, newest first. Creates the group if needed."""
        version, groups = self._groups(http)
        found = next(
            (g for g in groups if g.get("name") == name and str(g.get("id", "")).startswith("0_")),
            None,
        )
        if found is not None:
            return str(found["id"]), parse_selfstock(str(found.get("content") or ""))
        created = self._group_call(
            http,
            "POST",
            "/group/v1/add",
            {"name": name, "type": "0", "version": version, "from": "sjcg_gphone"},
        )
        group_id = str(created.get("id") or "")
        if not group_id:
            raise TonghuashunError(f"同花顺建分组没有返回编号: {name}")
        logger.info("同花顺 created group %s", name)
        return group_id, []

    def _write(self, http: httpx.Client, path: str, group_id: str, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        version, _groups = self._groups(http)
        self._group_call(
            http,
            "POST",
            path,
            {
                "id": group_id,
                "content": format_selfstock(rows),
                "num": str(len(rows)),
                "version": version,
                "from": "sjcg_gphone",
            },
        )

    def _sync_groups_sync(self, keep_names: list[str]) -> dict[str, list[str]]:
        """Ensure rule groups + 今日自选 + 我的自选 exist. Never delete user-created groups."""
        keep = {
            _DAILY_WATCHLIST_GROUP,
            self._important_group,
            *(name.strip() for name in keep_names if name.strip()),
        }

        def work(http: httpx.Client) -> dict[str, list[str]]:
            created: list[str] = []
            present = {str(g.get("name") or "") for g in self._custom_groups(http)}
            for name in sorted(keep):
                if name not in present:
                    self._group(http, name)
                    created.append(name)
            return {"keep": sorted(keep), "created": created, "deleted": []}

        return self._with_session(work)

    def _clear_day_sync(self, names: list[str], day: str) -> list[str]:
        """Clear only the given session groups when their stored day is not ``day``."""
        targets = {name.strip() for name in names if name.strip()}

        def work(http: httpx.Client) -> list[str]:
            cleared: list[str] = []
            days = self._group_days()
            for name in sorted(targets):
                if days.get(name) == day:
                    continue
                group_id, rows = self._group(http, name)
                self._clear_group_rows(http, name, group_id, rows, day)
                cleared.append(name)
            return cleared

        return self._with_session(work)

    def _members_sync(self, name: str, day: str, symbols: list[str], session: str) -> list[str]:
        def work(http: httpx.Client) -> list[str]:
            group_id, rows = self._group(http, name)
            days = self._group_days()
            if days.get(name) != day:
                self._clear_group_rows(http, name, group_id, rows, day)
                rows = []
            known = self._known_markets()
            for code, market in rows:
                known.setdefault(code.upper(), market)
            members = [code.upper() for code, _ in rows]
            for symbol in symbols:
                if symbol not in members:
                    self._market(http, symbol)
                    members.append(symbol)
            self._ensure_summary_group(
                http, _DAILY_WATCHLIST_GROUP, symbols, session, important=False
            )
            return members

        return self._with_session(work)

    def _add_summary_sync(
        self, symbols: list[str], session: str, *, important: bool
    ) -> None:
        fresh = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not fresh:
            return
        group_name = self._important_group if important else _DAILY_WATCHLIST_GROUP

        def work(http: httpx.Client) -> None:
            self._ensure_summary_group(
                http, group_name, fresh, session.strip(), important=important
            )

        self._with_session(work)

    def _arrange_sync(self, name: str, ordered: list[str]) -> None:
        def work(http: httpx.Client) -> None:
            group_id, rows = self._group(http, name)
            if [code.upper() for code, _ in rows] == ordered:
                return
            known = self._known_markets()
            # A batch add lands at the top in the order given, so clear and add the full list.
            self._write(http, "/content/v1/delete", group_id, rows)
            self._write(http, "/content/v1/add", group_id, [(code, known[code]) for code in ordered])
            logger.info("同花顺 %s: %s", name, ", ".join(ordered))

        self._with_session(work)

    async def members(self, group: str, day: str, symbols: list[str], session: str) -> list[str]:
        """Clears the group on a new day, then returns its names plus the new ones."""
        fresh = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        return await asyncio.to_thread(
            self._members_sync, group.strip(), day, fresh, session.strip()
        )

    async def arrange(self, group: str, ordered: list[str]) -> None:
        await asyncio.to_thread(self._arrange_sync, group.strip(), ordered)

    async def sync_groups(self, keep_names: list[str]) -> dict[str, list[str]]:
        """Create missing rule groups and delete unused custom groups."""
        return await asyncio.to_thread(self._sync_groups_sync, keep_names)

    async def clear_for_day(self, names: list[str], day: str) -> list[str]:
        """Empty yesterday's names from the given session groups only."""
        return await asyncio.to_thread(self._clear_day_sync, names, day)

    async def add_daily(self, symbols: list[str], session: str) -> None:
        """Keep names in 今日自选 without touching a rule group."""
        await asyncio.to_thread(self._add_summary_sync, symbols, session, important=False)

    async def add_important(self, symbols: list[str], session: str) -> None:
        """Also keep names in the important summary group (我的自选)."""
        await asyncio.to_thread(self._add_summary_sync, symbols, session, important=True)

    async def clear_watchlist_previous_session(self, current_session: str) -> list[str]:
        """Drop 今日自选 + 我的自选 names tagged with the previous session."""
        previous = _PREVIOUS_SESSION.get(current_session.strip())
        if not previous:
            return []
        from functools import partial

        daily = await asyncio.to_thread(
            partial(
                self._clear_summary_session_sync,
                previous,
                _DAILY_WATCHLIST_GROUP,
                important=False,
            )
        )
        important = await asyncio.to_thread(
            partial(
                self._clear_summary_session_sync,
                previous,
                self._important_group,
                important=True,
            )
        )
        # Preserve call-site logging: report union of removed symbols.
        seen: set[str] = set()
        ordered: list[str] = []
        for symbol in [*daily, *important]:
            if symbol not in seen:
                seen.add(symbol)
                ordered.append(symbol)
        return ordered

    def list_sync(self) -> list[tuple[str, str]]:
        return self._with_session(lambda http: self._query(http)[1])
