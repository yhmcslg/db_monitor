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


def _pick_index(datacenter: str | None, product: str | None) -> str:
    """按 datacenter + product 决定 ELK index pattern。

    规则：
      datacenter=ALi   + product=WFM3 → ali-mssqlslowlog_p-*
      datacenter=ALi   + 其他 product → ali-mysql_slow_log-*
      datacenter=huawei + product=WFM3 → hw-mssqlslowlog_p-*
      datacenter=huawei + 其他 product → hw-mysql_slow_log-*
    不匹配时回退到 config.yaml 里的 kibana.index_pattern。
    """
    dc = (datacenter or "").strip().lower()
    prod = (product or "").strip().lower()
    prefix = None
    if dc == "ali":
        prefix = "ali"
    elif dc == "huawei":
        prefix = "hw"
    if prefix is None:
        return get_config()["kibana"]["index_pattern"]
    if prod == "wfm3":
        return f"{prefix}-mssqlslowlog_p-*"
    return f"{prefix}-mysql_slow_log-*"


def _build_discover_path(users: list[str], index_id: str | None = None) -> str:
    """生成 /app/discover#/?... 里 hash 之后的完整路径（未 urlencode 的原始形态）。

    users 对应 filter 里的 params 与 should 子句。
    index_id 为空时回退到 config.yaml 里的默认 kibana.index_pattern。
    """
    cfg = get_config()["kibana"]
    if not index_id:
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


def build_long_url(
    users: list[str],
    datacenter: str | None = None,
    product: str | None = None,
) -> str:
    base = get_config()["kibana"]["base"].rstrip("/")
    index_id = _pick_index(datacenter, product)
    return base + _build_discover_path(users, index_id)


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
        r = s.get(base + "/login", timeout=30, allow_redirects=True)
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
            timeout=30,
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


def build_short_url(
    users: list[str],
    datacenter: str | None = None,
    product: str | None = None,
) -> tuple[str, str | None]:
    """返回 (final_url, error)。

    短链功能目前关闭（部署机器无法直接访问 ELK），直接返回长链，不走任何 API。
    error 固定为 None，上层 main.py 据此跳过"把长链 encode 展示"的分支。
    """
    base = get_config()["kibana"]["base"].rstrip("/")
    index_id = _pick_index(datacenter, product)
    return base + _build_discover_path(users, index_id), None
