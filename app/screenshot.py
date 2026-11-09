"""Grafana 截图：Playwright + 单 panel d-solo + Pillow 竖向拼接。

设计要点：
  - 整个进程共用一个 Chromium Browser（省启动开销）
  - 登录 cookie 持久化到 storage_state_path，session 有效期内复用
  - 用 Grafana 内置 /d-solo/<uid> 路径只渲染单个 panel（没有左边栏/顶栏干扰）
  - Pillow 拼接：顶上加一条标题带（实例类型 + 覆盖的库名），下面纵向拼 N 张 panel 图
  - 内存级 TTL 缓存：同一 (tenant, instance_key) 短期内复用图片
"""
from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from PIL import Image, ImageDraw, ImageFont

from .config import get_config
from .db import SchemaInfo
from .grafana import GrafanaLink, _ali_rds_prefix, _instance_key


ROOT = Path(__file__).resolve().parent.parent


# ------- 全局状态 -------

_browser = None            # type: ignore[assignment]
_playwright = None         # type: ignore[assignment]
_browser_lock = asyncio.Lock()
_cache: dict[tuple[str, str], tuple[float, bytes]] = {}


# ------- 入口 -------

async def render_instance_png(tenant: str, link: GrafanaLink, infos: list[SchemaInfo]) -> bytes:
    """为单个实例生成一张纵向拼接的 PNG。

    link: build_grafana_links 返回的某一项
    infos: 当前租户的全部 SchemaInfo（用于在截图里找对应原始 serverinnerip / name）

    截图顶部标题带会把 schema 名显示为业务名（考勤/排班/新人事/计算累计）。
    """
    cfg = get_config()["screenshot"]
    cache_key = (tenant, link.instance_type + "|" + link.instance_label)

    # 1. 查缓存
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < cfg["cache_ttl_sec"]:
        return cached[1]

    # 2. 找出这个实例在 infos 里对应的一条，作为变量参数来源
    rep = next((i for i in infos if i.schemaname in link.schemas), None)
    if rep is None:
        raise RuntimeError(f"找不到实例对应的 SchemaInfo: {link.instance_label}")

    # 3. 查 panel 配置
    panels_cfg = get_config()["grafana"]["panels"].get(rep.instance_type)
    if not panels_cfg:
        raise RuntimeError(f"config.yaml 未配置 {rep.instance_type} 的 panels")

    # 4. 组装每个 panel 的 d-solo URL
    uid = get_config()["grafana"]["dashboards"][rep.instance_type]
    base_vars = _panel_vars(rep)

    # 对 PolarDB / TaurusDB 这种 nodeId 必须具体值的面板，先 probe 真实 nodeId
    base_vars = await _resolve_node_id(uid, base_vars, rep.instance_type)

    panel_items: list[tuple[str, str]] = []  # [(标题, URL)]
    for display, panel_id in panels_cfg.items():
        url = _solo_url(uid, panel_id, base_vars)
        panel_items.append((display, url))

    # 5. 用 Playwright 逐个截图
    panel_pngs = await _screenshot_panels(panel_items)

    # 6. Pillow 拼接
    png = _compose(link, panel_pngs, tenant)
    _cache[cache_key] = (now, png)
    return png


# ------- Playwright 封装 -------

async def _get_browser():
    global _browser, _playwright
    async with _browser_lock:
        if _browser is None:
            from playwright.async_api import async_playwright
            _playwright = await async_playwright().start()
            cfg_s = get_config()["screenshot"]
            launch_args = {
                "headless": cfg_s["headless"],
                # 关掉沙箱，Windows 服务器 / Docker 常见，避免 spawn UNKNOWN
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            # 可选：指定用系统 Chrome / Edge（当 bundled chromium 被杀软拦截时很有用）
            channel = cfg_s.get("channel")
            if channel:
                launch_args["channel"] = channel
            _browser = await _playwright.chromium.launch(**launch_args)
        return _browser


async def _new_context():
    """返回一个 BrowserContext：优先加载已有 storage_state，没有则会让首次调用触发登录。"""
    cfg = get_config()["screenshot"]
    browser = await _get_browser()
    state_path = (ROOT / cfg["storage_state_path"]).resolve()
    # viewport 稍大一些，让 Grafana 有空间渲染；最后截的是 panel-container 这个元素本身，
    # 元素本身尺寸由 Grafana 的 d-solo 布局决定，和 viewport 高度无关。
    viewport = {"width": max(cfg["panel_width"], 1000), "height": 800}
    if state_path.exists():
        return await browser.new_context(
            storage_state=str(state_path),
            viewport=viewport,
            ignore_https_errors=True,
        )
    return await browser.new_context(
        viewport=viewport,
        ignore_https_errors=True,
    )


async def _ensure_logged_in(context) -> None:
    """走 Tornado 自建网关登录：GET /login 拿 _xsrf+cookie → POST /login →
    把 session cookie 塞给 Playwright context，之后截图自动带 cookie 过网关。
    """
    import re as _re
    import requests as _requests
    from urllib.parse import urlparse

    cfg_g = get_config()["grafana"]
    cfg_s = get_config()["screenshot"]
    base = cfg_g["base"].rstrip("/")
    user = cfg_g["username"]
    pwd = cfg_g["password"]

    sess = _requests.Session()
    # 1. GET /login 拿 _xsrf 和初始 cookie
    r = sess.get(base + "/login", timeout=10, allow_redirects=True, verify=True)
    if r.status_code != 200:
        raise RuntimeError(f"Grafana GET /login 失败：HTTP {r.status_code}")
    m = _re.search(r'name="_xsrf"\s+value="([^"]+)"', r.text)
    if not m:
        # 如果不是 Tornado 登录页，可能就是 Grafana 原生登录页，用老办法
        return await _grafana_native_login(context)
    xsrf = m.group(1)

    # 2. POST /login
    r2 = sess.post(
        base + "/login",
        data={"_xsrf": xsrf, "username": user, "password": pwd},
        timeout=10,
        allow_redirects=True,
        verify=True,
    )
    low = r2.text[:800].lower()
    if "<form" in low and "_xsrf" in low and "password" in low:
        raise RuntimeError("Grafana 网关登录失败：账号或密码错误（返回还是登录页）")

    # 3. 把 cookie 塞给 Playwright context
    host = urlparse(base).hostname
    cookies_for_pw = []
    for c in sess.cookies:
        cookies_for_pw.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or host,
            "path": c.path or "/",
            "secure": bool(c.secure),
            "httpOnly": bool(c._rest.get("HttpOnly", False)) if hasattr(c, "_rest") else False,
        })
    if cookies_for_pw:
        await context.add_cookies(cookies_for_pw)

    # 4. 存一下 state，下次冷启动直接用
    await context.storage_state(path=str((ROOT / cfg_s["storage_state_path"]).resolve()))


async def _grafana_native_login(context) -> None:
    """没套网关、直接是 Grafana 原生登录页时的兜底方案。"""
    cfg_g = get_config()["grafana"]
    cfg_s = get_config()["screenshot"]
    page = await context.new_page()
    await page.goto(cfg_g["base"].rstrip("/") + "/login", wait_until="networkidle")
    await page.fill('input[name="user"], input[name="username"]', cfg_g["username"])
    await page.fill('input[name="password"]', cfg_g["password"])
    await page.locator('button[type="submit"]').first.click()
    try:
        await page.wait_for_url(lambda u: "/login" not in u, timeout=10000)
    except Exception:
        raise RuntimeError(f"Grafana 原生登录失败：当前 URL={page.url}")
    await context.storage_state(path=str((ROOT / cfg_s["storage_state_path"]).resolve()))
    await page.close()


async def _capture_one(context, title: str, url: str, timeout_ms: int) -> tuple[str, bytes]:
    """打开 d-solo URL，截 panel DOM 元素。"""
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # 等 panel 容器出现（Grafana 7+ 用 .panel-container；也兼容其他）
        container = None
        for sel in [".panel-container", "[data-panelid]", ".dashboard-solo"]:
            try:
                container = await page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
                if container:
                    break
            except Exception:
                continue

        # 等里面的图画出来：canvas 或 svg 里的 path
        try:
            await page.wait_for_function(
                """() => {
                    const c = document.querySelector('canvas');
                    if (c && c.width > 0 && c.height > 0) return true;
                    const path = document.querySelector('svg path');
                    if (path) return true;
                    // "No data" 也算渲染完成
                    const nd = document.querySelector('.panel-empty, .datapoints-warning');
                    return !!nd;
                }""",
                timeout=timeout_ms,
            )
        except Exception:
            pass

        # 稳一点，再给 Grafana 一拍把图例/坐标画全
        await page.wait_for_timeout(2000)

        if container:
            png = await container.screenshot()
        else:
            png = await page.screenshot(full_page=False)
        return (title, png)
    finally:
        await page.close()


async def _screenshot_panels(items: list[tuple[str, str]]) -> list[tuple[str, bytes]]:
    """并行截多个 panel，保留输入顺序返回。"""
    import asyncio as _aio
    cfg = get_config()["screenshot"]
    timeout_ms = cfg["render_timeout_sec"] * 1000

    context = await _new_context()
    try:
        # 探测登录状态：既看 URL 又看页面是否含登录表单
        probe_page = await context.new_page()
        await probe_page.goto(items[0][1], wait_until="domcontentloaded", timeout=timeout_ms)
        need_login = "/login" in probe_page.url
        if not need_login:
            html = (await probe_page.content())[:2000].lower()
            # Tornado 网关登录页特征：_xsrf + password 字段 + form
            if "_xsrf" in html and "password" in html and "<form" in html:
                need_login = True
        await probe_page.close()

        if need_login:
            await _ensure_logged_in(context)

        # 并行截图，保留顺序
        tasks = [_capture_one(context, t, u, timeout_ms) for t, u in items]
        return await _aio.gather(*tasks)
    finally:
        await context.close()


# ------- URL 组装 -------

# 探测到的 nodeId 缓存 10 分钟
_node_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def _resolve_node_id(
    uid: str, base_vars: dict[str, str], instance_type: str,
) -> dict[str, str]:
    """补上 Grafana 面板里 nodeId 这种"必须具体值"的变量。

    - 阿里 PolarDB：直接调阿里云 Open API 查 Writer 节点 ID（最可靠）
    - 华为 TaurusDB：目前还是走 Playwright probe（后续有华为 SDK 可替换）
    - 其他：不动
    """
    import asyncio as _aio
    import re as _re

    # 阿里 PolarDB：优先走 SDK
    if instance_type == "ali_polardb":
        cluster_desc = base_vars.get("var-DBClusterDescription") or ""
        if cluster_desc and cluster_desc != "All":
            # 现在的 base_vars 里如果已经显式填了具体 nodeId，就直接用
            if base_vars.get("var-nodeId", "All") not in ("", "All"):
                return base_vars
            # 调阿里云 SDK（同步调用，放线程池）
            from . import aliyun as _ali
            node_id = await _aio.to_thread(_ali.get_polardb_writer_node_id, cluster_desc)
            if node_id:
                return {**base_vars, "var-nodeId": node_id}
        # 兜底：SDK 拿不到就继续走下面的 Playwright probe
        var_key = "var-nodeId"
    elif instance_type == "hw_taurusdb":
        var_key = "var-gaussdb_mysql_node_id"
    else:
        return base_vars

    # 如果已经不是 "All"，直接用（给个显式值的话绕过探测）
    if base_vars.get(var_key, "All") != "All":
        return base_vars

    # 缓存 key：uid + 关键变量（如 DBClusterDescription / instance_name）
    cache_key_parts = [uid]
    for k in ("var-DBClusterDescription", "var-instance_name"):
        if k in base_vars:
            cache_key_parts.append(f"{k}={base_vars[k]}")
    cache_key = "|".join(cache_key_parts)

    now = time.time()
    cached = _node_cache.get(cache_key)
    if cached and now - cached[0] < 600:
        return {**base_vars, **cached[1]}

    # 打开面板总览页，让 Grafana 把下拉选默认值
    overview_url = _overview_url(uid, base_vars)
    cfg = get_config()["screenshot"]
    timeout_ms = cfg["render_timeout_sec"] * 1000

    context = await _new_context()
    try:
        # 探测登录状态
        page = await context.new_page()
        await page.goto(overview_url, wait_until="domcontentloaded", timeout=timeout_ms)
        html = (await page.content())[:2000].lower()
        if "/login" in page.url or ("_xsrf" in html and "<form" in html):
            await page.close()
            await _ensure_logged_in(context)
            page = await context.new_page()
            await page.goto(overview_url, wait_until="domcontentloaded", timeout=timeout_ms)

        # 等 Grafana 把下拉值初始化好
        try:
            await page.wait_for_function(
                f"""() => {{
                    const u = new URL(window.location.href);
                    const v = u.searchParams.get('{var_key}');
                    return v && v !== 'All' && v !== '';
                }}""",
                timeout=timeout_ms,
            )
        except Exception:
            pass
        # 再多等一拍
        await page.wait_for_timeout(1000)

        current = page.url
        await page.close()

        m = _re.search(fr"[?&]{_re.escape(var_key)}=([^&]+)", current)
        if m:
            resolved = {var_key: m.group(1)}
            _node_cache[cache_key] = (now, resolved)
            return {**base_vars, **resolved}
    finally:
        await context.close()

    # 探测失败就退回原值；Grafana 会显示 No data，但至少图能截出来
    return base_vars


def _overview_url(uid: str, variables: dict[str, str]) -> str:
    """面板总览 URL（不带 panelId），用于让 Grafana 填默认变量。"""
    cfg_g = get_config()["grafana"]
    base = cfg_g["base"].rstrip("/")
    qs: dict[str, str] = {
        "orgId": "1",
        "from": cfg_g["default_from"],
        "to": cfg_g["default_to"],
        "var-interval": "1m",
        "var-datasource": "Prometheus",
    }
    qs.update(variables)
    from urllib.parse import urlencode
    return f"{base}/d/{uid}/?{urlencode(qs)}"


def _solo_url(uid: str, panel_id: int, variables: dict[str, str]) -> str:
    cfg_g = get_config()["grafana"]
    base = cfg_g["base"].rstrip("/")
    qs: dict[str, str] = {
        "orgId": "1",
        "from": cfg_g["default_from"],
        "to": cfg_g["default_to"],
        "panelId": str(panel_id),
        "var-interval": "1m",
        "var-datasource": "Prometheus",
    }
    qs.update(variables)
    # 单 panel 视图：Grafana 里是 /d-solo/<uid>
    return f"{base}/d-solo/{uid}/?{urlencode(qs)}"


def _panel_vars(info: SchemaInfo) -> dict[str, str]:
    """根据实例类型填该面板需要的 var-*。与 grafana.py 里的逻辑保持一致。"""
    t = info.instance_type
    if t == "ali_rds":
        return {
            "var-RegionId": "All", "var-Engine": "All",
            "var-DBInstanceDescription": "All",
            "var-instanceId": _ali_rds_prefix(info.serverinnerip) or "All",
        }
    if t == "ali_polardb":
        return {
            "var-RegionId": "All", "var-DBType": "MySQL",
            "var-DBClusterDescription": info.name or "All",
            "var-DBNodeRole": "Writer", "var-nodeId": "All",
        }
    if t == "ali_ecs":
        return {
            "var-RegionId": "All", "var-Env": "All", "var-Product_Line": "All",
            "var-Server_Type": "All", "var-OSType": "All",
            "var-PrivateIpAddress": info.serverinnerip or "All",
            "var-PublicIpAddress": "All", "var-InstanceName": "All", "var-instanceId": "All",
        }
    if t == "hw_taurusdb":
        node_name = f"{info.name}_node01" if info.name else "All"
        node_id = "All"
        if info.name:
            try:
                from .huaweicloud import get_taurusdb_master_node_id
                node_id = get_taurusdb_master_node_id(info.name) or "All"
            except Exception:  # noqa: BLE001
                pass
        return {
            "var-datasource": "Prometheus-JVM",
            "var-instance_name": info.name or "All",
            "var-gaussdb_mysql_instance_id": "All",
            "var-node_name": node_name,
            "var-gaussdb_mysql_node_id": node_id,
        }
    if t == "hw_rds":
        return {
            "var-Env": "All", "var-Product_Line": "All", "var-db_type": "All",
            "var-private_ip": info.serverinnerip or "All",
            "var-public_ip": "All", "var-name": info.name or "All",
            "var-instance_id": "All",
        }
    if t == "hw_ecs":
        return {
            "var-Env": "All", "var-Product_Line": "All", "var-Server_Type": "All",
            "var-os_type": "All",
            "var-private_ip_address": info.serverinnerip or "All",
            "var-public_ip_address": "All", "var-name": info.name or "All",
            "var-instance_id": "All",
        }
    return {}


# ------- 拼接 -------

_FONT: Optional[ImageFont.FreeTypeFont] = None


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """尽量加载 Pillow 内置字体（跨平台可用）；失败回退默认。"""
    global _FONT
    # PILL 从 10 之后带了 DejaVu，但不支持中文；中文字符会被画成方块。
    # 先尝试几个常见中文字体路径，找到就用；都没有就退回默认。
    candidates = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        # Windows 中文字体
        "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/simhei.ttf",     # 黑体
        "C:/Windows/Fonts/simsun.ttc",     # 宋体
        "C:/Windows/Fonts/simsun.ttf",
        # Linux
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _compose(link: GrafanaLink, panel_pngs: list[tuple[str, bytes]], tenant: str) -> bytes:
    cfg = get_config()["screenshot"]
    pw = cfg["panel_width"]

    header_h = 60
    sub_title_h = 28
    title_font = _get_font(18)
    sub_font = _get_font(13)

    # 预先加载 & 等宽缩放，拿到每张的真实高度
    images: list[tuple[str, Image.Image]] = []
    total_panel_h = 0
    for name, data in panel_pngs:
        img = Image.open(io.BytesIO(data))
        if img.width != pw:
            scale = pw / img.width
            img = img.resize((pw, int(img.height * scale)))
        images.append((name, img))
        total_panel_h += sub_title_h + img.height

    total_h = header_h + total_panel_h
    canvas = Image.new("RGB", (pw, total_h), "white")
    draw = ImageDraw.Draw(canvas)

    # 顶部标题
    draw.rectangle([(0, 0), (pw, header_h)], fill="#1f2937")
    draw.text((16, 10), f"{link.instance_label}  [{link.instance_type}]",
              fill="white", font=title_font)
    from .config import format_schemas
    labels = format_schemas(link.schemas, tenant)
    sub_line = "库: " + " / ".join(labels)
    if link.note:
        # note 原文里是 schema 名，用业务名替换一下（避免原 schema 混入截图）
        sub_line = link.note.replace(", ".join(link.schemas), "、".join(labels)) + "   " + sub_line
    draw.text((16, 35), sub_line, fill="#d1d5db", font=sub_font)

    y = header_h
    for name, img in images:
        draw.rectangle([(0, y), (pw, y + sub_title_h)], fill="#f3f4f6")
        draw.text((12, y + 6), name, fill="#111827", font=sub_font)
        y += sub_title_h
        canvas.paste(img, (0, y))
        y += img.height

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


# ------- 关闭 -------

async def shutdown():
    global _browser, _playwright
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
