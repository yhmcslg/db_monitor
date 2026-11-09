"""冒烟测试：mock 不同场景的 info 表数据，验证类型识别、URL 构造、同实例合并。

不连 MySQL，不调 Kibana。直接运行：python tests/smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import SchemaInfo, _classify  # noqa: E402
from app.elk import build_long_url         # noqa: E402
from app.grafana import build_grafana_links  # noqa: E402


def _mk(row: dict) -> SchemaInfo:
    return SchemaInfo(
        schemaname=row["schemaname"],
        product=row.get("product"),
        env=row.get("env"),
        serverinnerip=row.get("serverinnerip"),
        serverport=row.get("serverport"),
        serverouterip=row.get("serverouterip"),
        serverouterport=row.get("serverouterport"),
        name=row.get("name"),
        datacenter=row.get("datacenter"),
        instance_type=_classify(row),
    )


def test_classify():
    # 阿里
    assert _classify({"datacenter": "ALi",    "instance_type": "RDS"})     == "ali_rds"
    assert _classify({"datacenter": "ALi",    "instance_type": "polardb"}) == "ali_polardb"
    assert _classify({"datacenter": "ALi",    "instance_type": "ECS"})     == "ali_ecs"
    # 华为
    assert _classify({"datacenter": "huawei", "instance_type": "TaurusDB"}) == "hw_taurusdb"
    assert _classify({"datacenter": "huawei", "instance_type": "RDS"})      == "hw_rds"
    assert _classify({"datacenter": "huawei", "instance_type": "ecs"})      == "hw_ecs"
    # 异常兜底
    assert _classify({"datacenter": "aws",    "instance_type": "ECS"})      == "unknown"
    assert _classify({"datacenter": "ALi",    "instance_type": ""})         == "unknown"
    print("✔ classify 规则正确")


def test_four_schemas_same_ali_rds():
    """最常见场景：4 个库都在同一台阿里 RDS 上，只给一条链接并注明。"""
    rows = []
    for s in ["geely", "schedule_geely", "corehr_geely", "gaia_acc_geely"]:
        rows.append({
            "schemaname": s, "product": "WFM", "env": "Product",
            "serverinnerip": "rm-uf6aaa.mysql.rds.aliyuncs.com", "serverport": 3306,
            "serverouterip": None, "serverouterport": None,
            "name": "Prod_E2_WFMx_Geely_RDS", "datacenter": "ALi",
            "instance_type": "RDS",
        })
    infos = [_mk(r) for r in rows]
    links = build_grafana_links(infos)
    assert len(links) == 1, f"预期合并成 1 条，实际 {len(links)}"
    link = links[0]
    assert link.instance_type == "ali_rds"
    assert link.note and "4" in link.note
    assert "rm-uf6aaa" in link.url
    print("✔ 同实例合并 + note 正确")
    print("  示例 Grafana URL:", link.url)


def test_mixed_instances():
    """混合场景：2 库在 RDS、1 库在 PolarDB、1 库在华为 TaurusDB。"""
    rows = [
        {"schemaname": "geely", "serverinnerip": "rm-uf6bbb.mysql.rds.aliyuncs.com",
         "name": "Prod_Geely_RDS", "datacenter": "ALi", "instance_type": "RDS"},
        {"schemaname": "schedule_geely", "serverinnerip": "rm-uf6bbb.mysql.rds.aliyuncs.com",
         "name": "Prod_Geely_RDS", "datacenter": "ALi", "instance_type": "RDS"},
        {"schemaname": "corehr_geely", "serverinnerip": "pc-uf6ccc.polardb.rds.aliyuncs.com",
         "name": "Prod_Geely_Polardb", "datacenter": "ALi", "instance_type": "PolarDB"},
        {"schemaname": "gaia_acc_geely", "serverinnerip": "10.215.2.50",
         "name": "Gaussdb_Prod_SH_TaurusDB_Geely", "datacenter": "huawei", "instance_type": "TaurusDB"},
    ]
    infos = [_mk(r) for r in rows]
    links = build_grafana_links(infos)
    assert len(links) == 3, f"预期 3 条，实际 {len(links)}"
    by_type = {l.instance_type: l for l in links}
    assert "ali_rds" in by_type
    assert "ali_polardb" in by_type
    assert "hw_taurusdb" in by_type
    assert len(by_type["ali_rds"].schemas) == 2
    assert "var-DBClusterDescription=Prod_Geely_Polardb" in by_type["ali_polardb"].url
    assert "var-datasource=Prometheus-JVM" in by_type["hw_taurusdb"].url
    assert "var-instance_name=Gaussdb_Prod_SH_TaurusDB_Geely" in by_type["hw_taurusdb"].url
    print("✔ 混合实例聚合正确")


def test_ecs_url_shape():
    rows = [{"schemaname": "geely", "serverinnerip": "10.20.0.239",
             "name": "Prod_E2_WFMx_PT_DB_MASTER", "datacenter": "ALi",
             "instance_type": "ECS"}]
    infos = [_mk(r) for r in rows]
    link = build_grafana_links(infos)[0]
    assert "var-PrivateIpAddress=10.20.0.239" in link.url
    print("✔ 阿里 ECS URL 包含 PrivateIpAddress 变量")


def test_elk_url():
    url = build_long_url(["geely", "schedule_geely", "corehr_geely", "gaia_acc_geely"])
    # 需要 index pattern、4 个用户都出现在 rison 里
    assert "/app/discover" in url
    assert "geely" in url and "schedule_geely" in url
    assert "corehr_geely" in url and "gaia_acc_geely" in url
    print("✔ ELK 长链生成正确")
    print("  长链前 120 字:", url[:120], "...")


if __name__ == "__main__":
    test_classify()
    test_four_schemas_same_ali_rds()
    test_mixed_instances()
    test_ecs_url_shape()
    test_elk_url()
    print("\n全部冒烟通过 ✅")
