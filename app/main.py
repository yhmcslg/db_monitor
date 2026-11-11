"""FastAPI 入口：网页表单 + JSON API + Grafana 截图。"""
from __future__ import annotations

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from .config import derive_schemas, format_schemas, get_config
from .db import query_schemas
from .elk import build_short_url
from .grafana import build_grafana_links


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="db_monitor", description="按租户代码拼 Grafana 监控 + ELK 慢 SQL 短链")


def _lookup(tenant_code: str) -> dict:
    tenant_code = tenant_code.strip()
    schemas = derive_schemas(tenant_code)
    infos = query_schemas(tenant_code)

    found_names = {i.schemaname for i in infos}
    missing = [s for s in schemas if s not in found_names]

    grafana_links = build_grafana_links(infos)
    # 用第一条记录的 datacenter + product 决定 ELK index（WFM3 走 MSSQL，其他走 MySQL）
    dc = infos[0].datacenter if infos else None
    product = infos[0].product if infos else None
    elk_url, elk_err = build_short_url(schemas, dc, product)

    screenshot_enabled = get_config().get("screenshot", {}).get("enabled", False)

    return {
        "tenant": tenant_code,
        "schemas": schemas,
        "schema_labels": format_schemas(schemas, tenant_code),
        "missing_schemas": missing,
        "instances": [i.to_dict() for i in infos],
        "grafana": [
            {
                "url": g.url,
                "instance_type": g.instance_type,
                "instance_label": g.instance_label,
                "schemas": g.schemas,
                "schema_labels": format_schemas(g.schemas, tenant_code),
                "note": g.note,
                "screenshot_key": _screenshot_key(g),
            }
            for g in grafana_links
        ],
        "elk": {
            "url": elk_url,
            # 把 ! ( ) ' , 等字符 URL-encode，避免客户端"自动识别超链接"被截断
            "display_url": _encode_elk_long_url(elk_url),
            "error": elk_err,
        },
        "screenshot_enabled": screenshot_enabled,
    }


def _screenshot_key(link) -> str:
    """稳定、可读的 key，用来在 /api/screenshot 里反查这条链接。"""
    return f"{link.instance_type}|{link.instance_label}"


def _build_instance_card(
    g: dict,
    image_keys: list,
    screenshot_err: str | None,
    *,
    elk_url: str | None = None,
    tenant: str | None = None,
) -> dict:
    """为一个 Grafana 实例构造飞书消息卡片 JSON。

    g: 来自 _lookup 返回里的 grafana[] 单项
    image_keys: [(panel_name, image_key), ...]，可为空
    screenshot_err: 截图失败时的错误消息；无则 None
    elk_url: 如果给了，就在卡片最顶部插入一行 "打开 ELK 慢 SQL" 超链接（通常第一张卡片带）
    tenant: 如果给了，就在 ELK 行前面加 "租户：xxx"
    """
    elements: list = []

    # 可选：顶部塞 ELK 链接 + 分割线（给第一张卡片）
    if elk_url:
        tenant_line = f"**租户：{tenant}**\n" if tenant else ""
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{tenant_line}所有库的 ELK 慢 SQL 地址：[打开 ELK 慢 SQL]({elk_url})",
            },
        })
        elements.append({"tag": "hr"})

    # note + Grafana 链接合成一行；标题已经在 header 显示
    note_part = g.get("note") or ("库：" + " / ".join(g.get("schema_labels") or []))
    one_line = f"{note_part}  [打开 Grafana 面板]({g['url']})"
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": one_line},
    })

    # 分割线
    if image_keys or screenshot_err:
        elements.append({"tag": "hr"})

    # 图片横排：每 3 张一行（用 column_set 列布局）
    # 每列 width=weighted/weight=1 → 等宽均分
    PER_ROW = 3
    for start in range(0, len(image_keys), PER_ROW):
        row = image_keys[start:start + PER_ROW]
        columns = []
        for name, key in row:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**{name}**"}},
                    {
                        "tag": "img",
                        "img_key": key,
                        "alt": {"tag": "plain_text", "content": name},
                        "mode": "fit_horizontal",
                        "preview": True,  # 点击弹大图
                    },
                ],
            })
        # 不足 3 张时补空列，保持宽度一致
        while len(columns) < PER_ROW:
            columns.append({
                "tag": "column", "width": "weighted", "weight": 1,
                "elements": [],
            })
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": columns,
        })

    if screenshot_err:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"⚠️ 截图失败：{screenshot_err}"},
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"{g['instance_label']}  [{g['instance_type']}]",
            },
        },
        "elements": elements,
    }


def _encode_elk_long_url(url: str) -> str:
    """把 ELK discover 长链里会被客户端"识别超链接"截断的字符做百分号编码。

    Kibana 的 _g=(...) / _a=(...) 原生格式里有 ! ( ) ' , 这些字符，
    飞书 / 微信 / 钉钉等很多富文本客户端的自动识别逻辑看到 ( 就会把链接截断，
    导致用户点击只能跳到一半的 URL。把它们编码成 %xx 后客户端还能识别全长。
    浏览器打开时会自动 decode 回去，不影响 Kibana 解析。
    """
    if not url:
        return url
    # 保留 scheme、host、路径、#，只对 hash 后面的参数部分转义
    if "#" not in url:
        return url
    head, hash_part = url.split("#", 1)
    mapping = str.maketrans({
        "!": "%21",
        "(": "%28",
        ")": "%29",
        "'": "%27",
        ",": "%2C",
    })
    return head + "#" + hash_part.translate(mapping)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, tenant: str | None = Query(default=None)):
    result = None
    error = None
    if tenant:
        try:
            result = _lookup(tenant)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "tenant": tenant or "", "result": result, "error": error},
    )


@app.get("/api/lookup")
def api_lookup(tenant: str = Query(..., description="租户代码，如 geely")):
    try:
        return _lookup(tenant)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


@app.get("/api/screenshot")
async def api_screenshot(
    tenant: str = Query(..., description="租户代码"),
    key: str = Query(..., description="实例 key，形如 'ali_rds|阿里RDS rm-xxx'"),
):
    """按租户 + 实例 key 返回 Grafana 截图 PNG。"""
    if not get_config().get("screenshot", {}).get("enabled", False):
        return JSONResponse(status_code=404, content={"error": "screenshot 功能未启用"})

    try:
        from .screenshot import render_instance_png
        infos = query_schemas(tenant.strip())
        links = build_grafana_links(infos)
        link = next((l for l in links if _screenshot_key(l) == key), None)
        if link is None:
            return JSONResponse(status_code=404, content={"error": f"实例 key 不存在: {key}"})
        png = await render_instance_png(tenant, link, infos)
        return Response(content=png, media_type="image/png")
    except Exception as exc:  # noqa: BLE001
        import traceback
        _log(f"[screenshot error] tenant={tenant} key={key}\n" + traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


import logging
_logger = logging.getLogger("db_monitor")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_h)
    _logger.propagate = False


def _log(msg: str) -> None:
    """走 logging 库，避免 Windows cmd 里 print 到 stderr 被各种吞。"""
    try:
        _logger.info(msg)
    except Exception:
        pass


@app.post("/feishu/webhook")
async def feishu_webhook(request: Request, background: BackgroundTasks):
    """飞书事件回调。需在飞书事件配置里填这个 URL。"""
    import traceback
    # 整个 handler 外层 try：任何异常都返回 200，不让飞书重试打爆
    try:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            _log(f"[feishu] parse json failed: {exc}")
            return {"ok": True}
        event_type = ((body.get("header") or {}).get("event_type")
                      or (body.get("event") or {}).get("type")
                      or body.get("type"))
        _log(f"[feishu] webhook hit: type={event_type} body_keys={list(body.keys())}")
    except Exception:  # noqa: BLE001
        _log("[feishu] webhook early exception:\n" + traceback.format_exc())
        return {"ok": True}

    try:
        # 1) URL 验证：原样返回 challenge
        if body.get("type") == "url_verification":
            _log(f"[feishu] url_verification challenge={body.get('challenge')}")
            return {"challenge": body.get("challenge")}

        # 2) Token 校验（1.0 schema 在顶层 token；2.0 schema 在 header.token）
        cfg_fs = get_config().get("feishu", {})
        expected = cfg_fs.get("verification_token") or ""
        got = body.get("token") or (body.get("header") or {}).get("token") or ""
        if expected and got and got != expected:
            _log(f"[feishu] token mismatch (expect={expected[:4]}..., got={got[:4]}...)")
            return JSONResponse(status_code=401, content={"error": "token mismatch"})

        # 3) 消息事件：同步只做"收到了"的回执，重活扔后台
        if event_type == "im.message.receive_v1":
            from . import feishu as fs
            msg = (body.get("event") or {}).get("message") or {}
            chat_type = msg.get("chat_type")
            _log(f"[feishu] im.message.receive_v1 chat_id={msg.get('chat_id')} "
                 f"chat_type={chat_type} message_type={msg.get('message_type')} "
                 f"content={msg.get('content')}")
            if chat_type == "group" and not fs.is_bot_mentioned(body):
                _log("[feishu] group message without bot mention, ignore")
                return {"ok": True}
            tenant = fs.extract_tenant_code(body)
            _log(f"[feishu] extracted tenant={tenant}")
            # 丢到独立线程里跑，避免阻塞 FastAPI 的 event loop
            # （Playwright async 本身要 loop，所以在线程里用 asyncio.run 起新的 loop）
            import threading
            threading.Thread(
                target=_handle_message_sync, args=(body,), daemon=True,
            ).start()
        else:
            _log(f"[feishu] unknown event_type={event_type}, ignore")
    except Exception:  # noqa: BLE001
        _log("[feishu] webhook unhandled exception:\n" + traceback.format_exc())
        # 不让 FastAPI 把 500 返回给飞书，否则飞书会重试
        return {"ok": True}
    return {"ok": True}


def _handle_message_sync(body: dict) -> None:
    """在独立线程里跑：起一个新的 asyncio loop 执行 _handle_message。

    这样一来 FastAPI 的主 event loop 不会被 Playwright/网络 IO 阻塞，
    新事件来了依然能被 handler 接住。
    """
    import asyncio, traceback
    try:
        asyncio.run(_handle_message(body))
    except Exception:  # noqa: BLE001
        _log("[feishu] _handle_message_sync crashed:\n" + traceback.format_exc())


async def _handle_message(body: dict) -> None:
    """异步后台任务：查数据 + 回复文本 + 发截图。

    必须是 async：render_instance_png 用了全局 Playwright/Browser，
    必须跑在 FastAPI 本身的 event loop 里，不能 asyncio.run() 另起 loop。
    """
    import traceback, asyncio
    _log("[feishu] _handle_message started")
    try:
        from . import feishu as fs
        tenant = fs.extract_tenant_code(body)
        chat_id = fs.get_chat_id(body)
        message_id = fs.get_message_id(body)
        if not chat_id:
            return
        if not tenant:
            if message_id:
                await asyncio.to_thread(
                    fs.reply_text, message_id,
                    "请在 @机器人 后输入租户代码，例如：@机器人 geely",
                )
            return

        # 查数据（同步，走线程池）
        data = await asyncio.to_thread(_lookup, tenant)

        # ELK 链接会塞到第一张实例卡片的顶部，不再单独发消息
        elk_url = data["elk"].get("display_url") or data["elk"]["url"]

        screenshot_enabled = data.get("screenshot_enabled", False)
        if screenshot_enabled:
            from .db import query_schemas
            from .grafana import build_grafana_links
            from .screenshot import render_instance_panels
            infos = await asyncio.to_thread(query_schemas, tenant.strip())
            links_for_shot = build_grafana_links(infos)
            # key: screenshot_key -> GrafanaLink 对象
            link_map = {_screenshot_key(l): l for l in links_for_shot}
        else:
            link_map = {}
            infos = []

        # 2. 对每个实例：组装一张消息卡片（链接 + 多张大图）一次发送
        # 第一张卡片顶部顺带带上 ELK 链接 + 分割线
        for idx, g in enumerate(data["grafana"]):
            link = link_map.get(g["screenshot_key"]) if screenshot_enabled else None
            # 2.1 上传所有 panel 图，拿 image_key
            image_keys: list[tuple[str, str]] = []  # [(name, image_key), ...]
            screenshot_err: str | None = None
            if screenshot_enabled and link is not None:
                try:
                    panels = await render_instance_panels(tenant, link, infos)
                    for name, png in panels:
                        key = await asyncio.to_thread(fs.upload_image, png)
                        image_keys.append((name, key))
                except Exception as exc:  # noqa: BLE001
                    screenshot_err = f"{type(exc).__name__}: {exc}"
                    _log(traceback.format_exc())

            # 2.2 组装卡片。第一张卡片带上 ELK 链接 + 租户名
            card = _build_instance_card(
                g, image_keys, screenshot_err,
                elk_url=elk_url if idx == 0 else None,
                tenant=tenant if idx == 0 else None,
            )
            await asyncio.to_thread(fs.send_card, chat_id, card)
    except Exception:  # noqa: BLE001
        _log("[feishu webhook error]\n" + traceback.format_exc())


@app.on_event("shutdown")
async def _shutdown():
    try:
        from .screenshot import shutdown
        await shutdown()
    except Exception:  # noqa: BLE001
        pass


@app.get("/healthz")
def healthz():
    return {"ok": True}


def main():
    import uvicorn
    cfg = get_config()["server"]
    uvicorn.run("app.main:app", host=cfg["host"], port=cfg["port"], reload=False)


if __name__ == "__main__":
    main()
