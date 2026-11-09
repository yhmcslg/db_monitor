"""FastAPI 入口：网页表单 + JSON API + Grafana 截图。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request
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
