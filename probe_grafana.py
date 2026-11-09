"""Grafana 能力探测脚本。

用法：
  1. 把账号密码填到 config.yaml 的 grafana 段（临时加 username/password 两个字段）
  2. python probe_grafana.py

它会告诉你：
  - 能不能用这个账号登录
  - Viewer 权限能不能调 /api/ds/query（走势图的基础）
  - 能不能自动抽取 6 个面板里的所有 PromQL 查询
  - 并把 PromQL 导出到 promql.yaml 供后续标注
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.config import get_config  # noqa: E402


def banner(msg: str) -> None:
    print(f"\n==== {msg} ====")


def main() -> int:
    cfg = get_config()["grafana"]
    base = cfg["base"].rstrip("/")
    user = cfg.get("username")
    pwd = cfg.get("password")
    token = cfg.get("token")
    verify = cfg.get("verify_ssl", True)

    session = requests.Session()
    session.verify = verify

    # ---- 1. 认证 ----
    banner("1. 认证")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        print("使用 Service Account token")
    elif user and pwd:
        # Grafana 支持 basic auth 直接调 API，先用这个最省事
        session.auth = (user, pwd)
        print(f"使用账号密码 basic auth: {user}")
    else:
        print("✗ config.yaml 的 grafana 段没有 token 也没有 username/password")
        print("  临时加两行：")
        print("  grafana:")
        print("    username: your-name")
        print("    password: your-password")
        return 1

    # 验证认证是否成功
    r = session.get(urljoin(base + "/", "api/user"), timeout=10)
    if r.status_code != 200:
        print(f"✗ /api/user 返回 HTTP {r.status_code}: {r.text[:200]}")
        print("  认证失败，检查账号密码或 token 是否正确")
        return 1
    me = r.json()
    print(f"✔ 登录成功：login={me.get('login')}, email={me.get('email')}, orgId={me.get('orgId')}")

    # ---- 2. 列数据源 ----
    banner("2. 查看数据源")
    r = session.get(urljoin(base + "/", "api/datasources"), timeout=10)
    if r.status_code == 403:
        # Viewer 可能没有列数据源的权限，试 /api/datasources/name/Prometheus
        print("  /api/datasources 403（Viewer 常见），尝试按名字查…")
        r2 = session.get(urljoin(base + "/", "api/datasources/name/Prometheus"), timeout=10)
        if r2.status_code == 200:
            ds = r2.json()
            print(f"✔ Prometheus 数据源可读: uid={ds.get('uid')}")
            prometheus_uid = ds.get("uid")
        else:
            print(f"  也失败：HTTP {r2.status_code}。先跳过")
            prometheus_uid = None
    elif r.status_code == 200:
        datasources = r.json()
        print(f"✔ 可列出 {len(datasources)} 个数据源")
        prometheus_uid = None
        for ds in datasources:
            if "prometheus" in (ds.get("type") or "").lower():
                print(f"   - {ds.get('name')} (uid={ds.get('uid')}, type={ds.get('type')})")
                if prometheus_uid is None:
                    prometheus_uid = ds.get("uid")
    else:
        print(f"✗ /api/datasources HTTP {r.status_code}")
        prometheus_uid = None

    # ---- 3. 读 6 个面板 JSON，抽 PromQL ----
    banner("3. 读取面板 JSON，抽取 PromQL")
    uids = cfg["dashboards"]
    all_promql: dict[str, list[dict]] = {}
    for label, uid in uids.items():
        r = session.get(urljoin(base + "/", f"api/dashboards/uid/{uid}"), timeout=15)
        if r.status_code != 200:
            print(f"   {label}({uid}): HTTP {r.status_code}  ✗")
            continue
        data = r.json()
        dash = data.get("dashboard") or {}
        panels = _flatten_panels(dash.get("panels") or [])
        exprs = []
        for p in panels:
            for t in (p.get("targets") or []):
                expr = t.get("expr")
                if expr:
                    exprs.append({
                        "panel_id": p.get("id"),
                        "panel_title": p.get("title"),
                        "ref_id": t.get("refId"),
                        "expr": expr,
                    })
        print(f"   {label}({uid}): {len(panels)} panel, 抽到 {len(exprs)} 条 PromQL  ✔")
        all_promql[label] = exprs

    # ---- 4. 试跑一条 query ----
    banner("4. 试跑一条 /api/ds/query")
    sample = None
    for label, exprs in all_promql.items():
        if exprs:
            sample = (label, exprs[0])
            break
    if sample and prometheus_uid:
        label, item = sample
        import time
        now = int(time.time() * 1000)
        body = {
            "queries": [{
                "refId": "A",
                "datasource": {"uid": prometheus_uid, "type": "prometheus"},
                "expr": item["expr"],
                "intervalMs": 60000,
                "maxDataPoints": 60,
            }],
            "from": str(now - 3600 * 1000),
            "to": str(now),
        }
        r = session.post(
            urljoin(base + "/", "api/ds/query"),
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            frames = r.json().get("results", {}).get("A", {}).get("frames", [])
            total_points = sum(len(f.get("data", {}).get("values", [[]])[0]) for f in frames)
            print(f"✔ 走势数据正常：面板={label}, panel={item['panel_title']}, "
                  f"返回 {len(frames)} 个 series，共 {total_points} 个数据点")
        else:
            print(f"✗ /api/ds/query HTTP {r.status_code}: {r.text[:300]}")
    else:
        print("  跳过：没拿到 Prometheus uid 或没抽到 PromQL")

    # ---- 5. 导出 promql.yaml ----
    banner("5. 导出 PromQL")
    out = ROOT / "promql.yaml"
    with out.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(all_promql, fp, allow_unicode=True, sort_keys=False)
    total = sum(len(v) for v in all_promql.values())
    print(f"✔ 写入 {out}，总 {total} 条 PromQL，你可以人工标注哪条是 CPU/连接/QPS/慢SQL")
    return 0


def _flatten_panels(panels: list[dict]) -> list[dict]:
    """Grafana 面板里 row 下可能嵌套 panels，拍平。"""
    out = []
    for p in panels:
        if (p.get("type") == "row") and p.get("panels"):
            out.extend(_flatten_panels(p["panels"]))
        else:
            out.append(p)
    return out


if __name__ == "__main__":
    sys.exit(main())
