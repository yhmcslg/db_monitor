"""查 devops.info 表，根据规则判定实例类型。"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal

from .config import get_config, derive_schemas


InstanceType = Literal[
    "ali_rds",      # 阿里云 RDS
    "ali_polardb",  # 阿里云 PolarDB
    "ali_ecs",      # 阿里云 ECS 自建
    "hw_taurusdb",  # 华为 TaurusDB
    "hw_rds",       # 华为 RDS
    "hw_ecs",       # 华为 ECS 自建
    "unknown",
]


@dataclass
class SchemaInfo:
    schemaname: str
    product: str | None
    env: str | None
    serverinnerip: str | None
    serverport: int | None
    serverouterip: str | None
    serverouterport: int | None
    name: str | None
    datacenter: str | None
    instance_type: InstanceType = "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(row: dict) -> InstanceType:
    """按 datacenter + instance_type 判定云厂商 + 实例类型（大小写不敏感）。

    取值约定：
      datacenter: ALi / huawei
      instance_type: ECS / RDS / PolarDB / TaurusDB
    """
    dc = (row.get("datacenter") or "").strip().lower()
    it = (row.get("instance_type") or "").strip().lower()

    if dc == "ali":
        if it == "ecs":
            return "ali_ecs"
        if it == "rds":
            return "ali_rds"
        if it == "polardb":
            return "ali_polardb"
        return "unknown"

    if dc == "huawei":
        if it == "ecs":
            return "hw_ecs"
        if it == "rds":
            return "hw_rds"
        if it == "taurusdb":
            return "hw_taurusdb"
        return "unknown"

    return "unknown"


def query_schemas(tenant_code: str) -> list[SchemaInfo]:
    """查 devops.info，返回 4 个业务库的信息（含识别出的类型）。"""
    import pymysql  # 延迟导入：冒烟测试不走这里也不必依赖 pymysql

    cfg = get_config()
    schemas = derive_schemas(tenant_code)
    schemas_sql_list = ", ".join(f"'{s}'" for s in schemas)
    sql = cfg["mysql"]["query_sql"].format(schemas=schemas_sql_list)

    conn = pymysql.connect(
        host=cfg["mysql"]["host"],
        port=cfg["mysql"]["port"],
        user=cfg["mysql"]["user"],
        password=cfg["mysql"]["password"],
        database=cfg["mysql"]["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()

    results: list[SchemaInfo] = []
    for row in rows:
        info = SchemaInfo(
            schemaname=row.get("schemaname"),
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
        results.append(info)

    # 按照 derive_schemas 的顺序排一下，没查到的 schema 不出现在结果里
    order = {s: i for i, s in enumerate(schemas)}
    results.sort(key=lambda r: order.get(r.schemaname, 999))
    return results
