"""列出 Kibana 里所有 index-pattern 的 title -> UUID 映射。

用法（在能访问 ELK 的 Mac / Windows 机器上跑）：

  .venv/bin/python tools/list_kibana_uuids.py

依赖 config.yaml 里 kibana.base/username/password。
走和主程序同一套 Tornado 网关登录逻辑。
"""
from __future__ import annotations

import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests

from app.config import get_config


def login_session() -> requests.Session:
    cfg = get_config()["kibana"]
    base = cfg["base"].rstrip("/")
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    if not user or not pwd:
        raise SystemExit("config.yaml 里 kibana.username / password 为空，请先填")

    s = requests.Session()
    r = s.get(base + "/login", timeout=15, allow_redirects=True)
    if r.status_code != 200:
        raise SystemExit(f"GET /login 失败：HTTP {r.status_code}")
    m = re.search(r'name="_xsrf"\s+value="([^"]+)"', r.text)
    if not m:
        raise SystemExit(f"找不到 _xsrf：{r.text[:300]}")
    xsrf = m.group(1)
    r2 = s.post(base + "/login",
                data={"_xsrf": xsrf, "username": user, "password": pwd},
                timeout=15, allow_redirects=True)
    low = r2.text[:800].lower()
    if "<form" in low and "_xsrf" in low and "password" in low:
        raise SystemExit(f"登录失败（账号或密码错）：status={r2.status_code}")
    return s


def main() -> int:
    cfg = get_config()["kibana"]
    base = cfg["base"].rstrip("/")
    s = login_session()

    # /api/saved_objects/_find 翻页抓全部 index-pattern
    all_items: list[dict] = []
    page = 1
    while True:
        r = s.get(
            base + "/api/saved_objects/_find",
            params={
                "type": "index-pattern",
                "per_page": 1000,
                "page": page,
                "fields": "title",
            },
            headers={"kbn-xsrf": "true"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:500]}")
            return 1
        data = r.json()
        batch = data.get("saved_objects", [])
        if not batch:
            break
        all_items.extend(batch)
        total = data.get("total", 0)
        print(f"page={page} got={len(batch)} cumulative={len(all_items)} total_declared={total}",
              file=sys.stderr)
        if len(all_items) >= total:
            break
        page += 1
        if page > 20:
            print("WARN: 已翻 20 页仍未取完，停止避免死循环", file=sys.stderr)
            break

    # 过滤出 slow log 相关的（你需要的那 4 个）
    print(f"\n{'UUID':<40} title")
    print("-" * 80)
    want = ("mysql_slow_log", "mssqlslowlog")
    hit = 0
    for item in all_items:
        title = (item.get("attributes") or {}).get("title", "")
        if any(w in title for w in want):
            uid = item.get("id", "")
            print(f"{uid:<40} {title}")
            hit += 1
    print(f"\n匹配到 {hit} 个 slow log 相关的 index-pattern（预期 4 个）")
    print(f"全部 index-pattern 总数：{len(all_items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
