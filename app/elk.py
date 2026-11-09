"""拼 ELK discover URL，并（可选）调 Kibana 短链 API 换成 goto/ 短链。

你们公司在 Kibana 前面套了一个 Tornado 自建登录网关：
  GET /login   → 返回 HTML，含 <input name="_xsrf" value="..."> 和初始 cookie
  POST /login  → 带 username/password/_xsrf 登录，拿到 session cookie
  登录后才能访问 /api/short_url 等 Kibana API。
"""
from __future__ import annotations

import re
import threading
import time
import warnings

import requests

from .config import get_config


_session: requests.Session | None = None
_session_ts: float = 0.0
_session_lock = threading.Lock()
_SESSION_TTL = 30 * 60  # 30 分钟后重新登录


def _rison_list(items: list[str]) -> str:
    """Rison 列表：!(a,b,c)，字符串如果含特殊字符要加引号，此处租户代码只有字母数字下划线，直接拼。"""
    return "!(" + ",".join(items) + ")"


def _build_discover_path(users: list[str]) -> str:
    """生成 /app/discover#/?... 里 hash 之后的完整路径（未 urlencode 的原始形态）。

    users 对应 filter 里的 params 与 should 子句。
    """
    cfg = get_config()["kibana"]
    index_id = cfg["index_pattern"]
    time_from = cfg["default_from"]
    time_to = cfg["default_to"]

    params_rison = _rison_list(users)
    should_rison = _rison_list([f"(match_phrase:(user:{u}))" for u in users])

    g = (
        f"(filters:!(),refreshInterval:(pause:!t,value:60000),"
        f"time:(from:{time_from},to:{time_to}))"
    )

    a = (
        "(columns:!(user,query_time,rows_sent,rows_examined,query),"
        "filters:!("
            "('$state':(store:appState),"
            "meta:(alias:!n,disabled:!f,"
                f"index:{index_id},"
                "key:user,negate:!f,"
                f"params:{params_rison},"
                "type:phrases),"
            "query:(bool:(minimum_should_match:1,"
                f"should:{should_rison}"
            "))"
            ")"
        "),"
        f"index:{index_id},"
        "interval:auto,"
        "query:(language:kuery,query:''),"
        "sort:!(!('@timestamp',desc)))"
    )

    # Kibana 的 hash URL 直接写原始 rison，不做 URL 编码（与手动复制的链接保持一致）
    return f"/app/discover#/?_g={g}&_a={a}"


def build_long_url(users: list[str]) -> str:
    base = get_config()["kibana"]["base"].rstrip("/")
    return base + _build_discover_path(users)


def _looks_like_login_html(text: str) -> bool:
    """判断响应是不是网关登录页。"""
    low = text[:800].lower()
    return "<form" in low and "_xsrf" in low and "password" in low


def _login(base: str, user: str, pwd: str, verify: bool) -> tuple[requests.Session | None, str | None]:
    """走 Tornado 自建登录网关，拿到带 session cookie 的 requests.Session。"""
    s = requests.Session()
    s.verify = verify
    # 1. GET /login 拿 _xsrf 和初始 cookie
    try:
        r = s.get(base + "/login", timeout=10, allow_redirects=True)
    except requests.RequestException as e:
        return None, f"GET /login: {e}"
    if r.status_code != 200:
        return None, f"GET /login: HTTP {r.status_code}"
    m = re.search(r'name="_xsrf"\s+value="([^"]+)"', r.text)
    if not m:
        return None, f"GET /login: 找不到 _xsrf 字段（前 200 字：{r.text[:200]}）"
    xsrf = m.group(1)

    # 2. POST /login
    try:
        r2 = s.post(
            base + "/login",
            data={"_xsrf": xsrf, "username": user, "password": pwd},
            timeout=10,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return None, f"POST /login: {e}"
    # 登录成功后网关会 302 到首页；如果失败通常返回 200 + 登录页本身
    if _looks_like_login_html(r2.text):
        return None, f"登录失败（账号或密码错）：status={r2.status_code}"

    return s, None


def _get_session(base: str, user: str, pwd: str, verify: bool) -> tuple[requests.Session | None, str | None]:
    """复用 session 30 分钟，到期重新登录。"""
    global _session, _session_ts
    with _session_lock:
        if _session is not None and time.time() - _session_ts < _SESSION_TTL:
            return _session, None
        s, err = _login(base, user, pwd, verify)
        if s is None:
            return None, err
        _session = s
        _session_ts = time.time()
        return s, None


def build_short_url(users: list[str]) -> tuple[str, str | None]:
    """返回 (final_url, error)。失败则 final_url 退化为长链，error 说明原因。"""
    cfg = get_config()["kibana"]
    base = cfg["base"].rstrip("/")
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    verify = cfg.get("verify_ssl", True)
    path = _build_discover_path(users)
    long_url = base + path

    if not user or not pwd:
        return long_url, "未配置 Kibana 账号密码，返回长链"

    if not verify:
        warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.InsecureRequestWarning)

    session, err = _get_session(base, user, pwd, verify)
    if session is None:
        return long_url, f"登录网关失败：{err}"

    endpoints_payloads = [
        # 新版 Kibana (7.14+)
        ("/api/short_url", {"locatorId": "LEGACY_SHORT_URL_LOCATOR", "params": {"url": path}}),
        # 简单版（有些版本也接受）
        ("/api/short_url", {"url": path}),
        # 老版 Kibana (7.13-)
        ("/api/shorten_url", {"url": path}),
    ]
    last_err: str | None = None

    for ep, payload in endpoints_payloads:
        try:
            resp = session.post(
                base + ep,
                json=payload,
                headers={"kbn-xsrf": "true", "Content-Type": "application/json"},
                timeout=10,
            )
        except requests.RequestException as e:
            last_err = f"{ep}: {e}"
            continue

        if _looks_like_login_html(resp.text):
            # session 可能被挤掉或过期：重登一次
            global _session
            with _session_lock:
                _session = None
            last_err = f"{ep}: 网关要求重新登录"
            continue

        if resp.status_code >= 400:
            last_err = f"{ep}: HTTP {resp.status_code} {resp.text[:200]}"
            continue

        try:
            data = resp.json()
        except ValueError:
            last_err = f"{ep}: 响应非 JSON：{resp.text[:200]}"
            continue

        # 兼容多种返回格式
        short_id = data.get("id") or data.get("urlId")
        if not short_id:
            last_err = f"{ep}: 响应里找不到 id：{str(data)[:200]}"
            continue
        return f"{base}/goto/{short_id}", None

    return long_url, last_err or "短链接口全部失败"
