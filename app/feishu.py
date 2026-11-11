"""飞书机器人：拿 token、发消息、上传图片、解析 @机器人 的文本。

事件流程：
  1. 飞书先发 url_verification，程序返回 challenge
  2. 之后发 im.message.receive_v1，程序解析出 @机器人 后面的租户代码
  3. 后台任务：查 Grafana 链接 + ELK 短链 + 截图，一并发回群里
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
from typing import Any

import requests

from .config import get_config


_token: str | None = None
_token_expire: float = 0.0
_token_lock = threading.Lock()


# ---------- token ----------

_bot_info: dict | None = None
_bot_info_ts: float = 0.0


import logging as _logging
_logger = _logging.getLogger("db_monitor")


def _log(msg: str) -> None:
    try:
        _logger.info(msg)
    except Exception:
        pass


def get_bot_info() -> dict:
    """拿当前机器人应用自身的信息（open_id、app_id 等），缓存 1 小时。"""
    global _bot_info, _bot_info_ts
    with _token_lock:
        if _bot_info and time.time() - _bot_info_ts < 3600:
            return _bot_info
        token = get_tenant_access_token()
        r = requests.get(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"拿 bot info 失败: {data}")
        # 响应里 bot 字段含 open_id、app_name
        _bot_info = data.get("bot") or {}
        _bot_info_ts = time.time()
        return _bot_info


def is_bot_mentioned(body: dict) -> bool:
    """判断消息里的 @ 列表里是否包含当前机器人。

    飞书 webhook 事件里，mention.id 只带 open_id/union_id（用户层），
    没有 app_id。但有两个字段可用：
      mentioned_type='bot'  → 这是对机器人的 @
      name='DB_Monitor'     → 机器人名
      id.open_id='ou_xxx'   → 机器人的 open_id
    优先级：配置里有 bot_open_id 就精确匹配；否则 name 匹配；
    都没配就按 mentioned_type=='bot' 放行（适合群里只有一个机器人）。
    """
    import sys
    cfg = get_config().get("feishu", {})
    bot_open_id = (cfg.get("bot_open_id") or "").strip()
    bot_name = (cfg.get("bot_name") or "").strip()
    msg = (body.get("event") or {}).get("message") or {}
    mentions = msg.get("mentions") or []
    _log(f"[feishu] mention check: bot_open_id={bot_open_id!r} "
         f"bot_name={bot_name!r} mentions={mentions}")
    if not mentions:
        return False
    for m in mentions:
        mtype = (m.get("mentioned_type") or "").lower()
        mid = m.get("id") or {}
        mopen = mid.get("open_id") if isinstance(mid, dict) else ""
        mname = (m.get("name") or "").strip()

        if bot_open_id:
            if mopen == bot_open_id:
                return True
            continue
        if bot_name:
            if mname == bot_name:
                return True
            continue
        # 都没配：是 @ 到机器人就放行
        if mtype == "bot":
            return True
    return False


def get_tenant_access_token() -> str:
    """缓存 90% 有效期的 tenant_access_token。"""
    global _token, _token_expire
    with _token_lock:
        if _token and time.time() < _token_expire:
            return _token
        cfg = get_config()["feishu"]
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"拿 tenant_access_token 失败: {data}")
        _token = data["tenant_access_token"]
        # expire 是秒；取 90% 作为真正过期时间
        _token_expire = time.time() + int(data.get("expire", 7200)) * 0.9
        return _token


# ---------- 事件处理 ----------

def is_url_verification(body: dict) -> bool:
    return body.get("type") == "url_verification"


def extract_tenant_code(body: dict) -> str | None:
    """从 im.message.receive_v1 里抽租户代码。"""
    msg = (body.get("event") or {}).get("message") or {}
    if msg.get("message_type") != "text":
        return None
    try:
        content = json.loads(msg.get("content") or "{}")
    except ValueError:
        return None
    text = content.get("text", "")
    # 文本形如 "@_user_1 geely" 或 "@机器人名 geely"，去掉 @xxx 后取第一个单词
    # 飞书 at 格式是 @_user_N，mentions 里有对应 key 和 name
    text = re.sub(r"@_user_\d+", "", text)  # 去掉 at 标记
    text = re.sub(r"@\S+", "", text)         # 兜底去掉 @名字
    text = text.strip()
    if not text:
        return None
    # 允许输入 "租户 geely" 或者单词 "geely"
    tokens = text.split()
    for t in reversed(tokens):
        if re.fullmatch(r"[A-Za-z0-9_\-]+", t):
            return t
    return None


def get_message_id(body: dict) -> str | None:
    return (((body.get("event") or {}).get("message") or {}).get("message_id"))


def get_chat_id(body: dict) -> str | None:
    return (((body.get("event") or {}).get("message") or {}).get("chat_id"))


# ---------- 发消息 ----------

def _post(url: str, payload: dict, token: str) -> dict:
    import sys
    r = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        timeout=15,
    )
    try:
        data = r.json()
    except Exception:
        data = {"_raw": r.text[:400], "_status": r.status_code}
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        _log(f"[feishu] POST {url} -> {r.status_code} {str(data)[:400]}")
    return data


def reply_text(message_id: str, text: str) -> dict:
    """回复到原消息（线程回复）。"""
    token = get_tenant_access_token()
    return _post(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
        {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        token,
    )


def send_text(chat_id: str, text: str) -> dict:
    token = get_tenant_access_token()
    return _post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        token,
    )


def upload_image(png_bytes: bytes) -> str:
    """上传图片，返回 image_key。"""
    token = get_tenant_access_token()
    r = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/images",
        headers={"Authorization": f"Bearer {token}"},
        data={"image_type": "message"},
        files={"image": ("screenshot.png", io.BytesIO(png_bytes), "image/png")},
        timeout=30,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"上传图片失败: {data}")
    return data["data"]["image_key"]


def send_image(chat_id: str, image_key: str) -> dict:
    token = get_tenant_access_token()
    return _post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        },
        token,
    )


def send_card(chat_id: str, card: dict) -> dict:
    """发送交互式消息卡片。card 是一个 schema=2.0 的卡片 JSON。"""
    token = get_tenant_access_token()
    return _post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        token,
    )
