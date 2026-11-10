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
    elk_url, elk_err = build_short_url(schemas)

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
        "elk": {"url": elk_url, "error": elk_err},
        "screenshot_enabled": screenshot_enabled,
    }


def _screenshot_key(link) -> str:
    """稳定、可读的 key，用来在 /api/screenshot 里反查这条链接。"""
    return f"{link.instance_type}|{link.instance_label}"


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
        import traceback, sys
        print(f"\n[screenshot error] tenant={tenant} key={key}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


@app.post("/feishu/webhook")
async def feishu_webhook(request: Request, background: BackgroundTasks):
    """飞书事件回调。需在飞书事件配置里填这个 URL。"""
    import sys
    body = await request.json()
    event_type = ((body.get("header") or {}).get("event_type")
                  or body.get("event", {}).get("type")
                  or body.get("type"))
    print(f"[feishu] webhook hit: type={event_type} body_keys={list(body.keys())}",
          file=sys.stderr, flush=True)

    # 1) URL 验证：原样返回 challenge
    if body.get("type") == "url_verification":
        print(f"[feishu] url_verification challenge={body.get('challenge')}",
              file=sys.stderr, flush=True)
        return {"challenge": body.get("challenge")}

    # 2) Token 校验（1.0 schema 在顶层 token；2.0 schema 在 header.token）
    cfg_fs = get_config().get("feishu", {})
    expected = cfg_fs.get("verification_token") or ""
    got = body.get("token") or (body.get("header") or {}).get("token") or ""
    if expected and got and got != expected:
        print(f"[feishu] token mismatch (expect={expected[:4]}..., got={got[:4]}...)",
              file=sys.stderr, flush=True)
        return JSONResponse(status_code=401, content={"error": "token mismatch"})

    # 3) 消息事件：同步只做"收到了"的回执，重活扔后台
    if event_type == "im.message.receive_v1":
        try:
            from . import feishu as fs
            msg = (body.get("event") or {}).get("message") or {}
            print(f"[feishu] im.message.receive_v1 chat_id={msg.get('chat_id')} "
                  f"message_type={msg.get('message_type')} content={msg.get('content')}",
                  file=sys.stderr, flush=True)
            tenant = fs.extract_tenant_code(body)
            print(f"[feishu] extracted tenant={tenant}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[feishu] pre-dispatch log failed: {exc}", file=sys.stderr, flush=True)
        background.add_task(_handle_message, body)
    else:
        print(f"[feishu] unknown event_type={event_type}, ignore", file=sys.stderr, flush=True)
    return {"ok": True}


async def _handle_message(body: dict) -> None:
    """异步后台任务：查数据 + 回复文本 + 发截图。

    必须是 async：render_instance_png 用了全局 Playwright/Browser，
    必须跑在 FastAPI 本身的 event loop 里，不能 asyncio.run() 另起 loop。
    """
    import sys, traceback, asyncio
    print("[feishu] _handle_message started", file=sys.stderr, flush=True)
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

        # 1. 文本消息：Grafana 链接 + ELK 短链
        lines = [f"租户：{tenant}", ""]
        lines.append("ELK 慢 SQL：")
        lines.append(data["elk"]["url"])
        if data["elk"].get("error"):
            lines.append(f"（短链失败：{data['elk']['error']}）")
        lines.append("")
        lines.append("Grafana 监控：")
        for g in data["grafana"]:
            lines.append(f"• {g['instance_label']} [{g['instance_type']}]")
            if g.get("note"):
                lines.append(f"  {g['note']}")
            lines.append(f"  库：{' / '.join(g['schema_labels'])}")
            lines.append(f"  {g['url']}")
        await asyncio.to_thread(fs.send_text, chat_id, "\n".join(lines))

        # 2. 每个实例一张截图
        if not data.get("screenshot_enabled"):
            return

        from .db import query_schemas
        from .grafana import build_grafana_links
        from .screenshot import render_instance_png

        infos = await asyncio.to_thread(query_schemas, tenant.strip())
        links = build_grafana_links(infos)
        for link in links:
            try:
                png = await render_instance_png(tenant, link, infos)
                image_key = await asyncio.to_thread(fs.upload_image, png)
                await asyncio.to_thread(fs.send_image, chat_id, image_key)
            except Exception as exc:  # noqa: BLE001
                await asyncio.to_thread(
                    fs.send_text, chat_id,
                    f"[{link.instance_label}] 截图失败：{type(exc).__name__}: {exc}",
                )
                traceback.print_exc(file=sys.stderr)
    except Exception:  # noqa: BLE001
        print("\n[feishu webhook error]", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


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
