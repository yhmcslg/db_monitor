"""根据 SchemaInfo 拼 Grafana 监控 URL；4 个库若在同一实例上则合并为一条。"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from .config import get_config
from .db import SchemaInfo


@dataclass
class GrafanaLink:
    url: str
    instance_type: str
    instance_label: str          # 便于人看，如 "阿里RDS rm-uf6xxx"
    schemas: list[str]           # 这条链接覆盖的库
    note: str | None = None      # 比如 "4 个业务库都在这个实例上"


# ---------- 内部：各类型的 key 与 URL 构造 ----------

def _ali_rds_prefix(ip: str | None) -> str:
    # serverinnerip 形如 rm-uf6xxx.mysql.rds.aliyuncs.com，取第一个 . 前的部分
    if not ip:
        return ""
    return ip.split(".", 1)[0]


def _instance_key(info: SchemaInfo) -> tuple[str, str]:
    """用来判断"同一实例"的键。"""
    t = info.instance_type
    if t == "ali_rds":
        return (t, _ali_rds_prefix(info.serverinnerip))
    if t == "ali_polardb":
        # 先按 name 聚合（Grafana 里用 DBClusterDescription=name 匹配），name 为空退化到 IP 前缀
        return (t, (info.name or _ali_rds_prefix(info.serverinnerip)))
    if t == "ali_ecs":
        return (t, info.serverinnerip or "")
    if t == "hw_taurusdb":
        return (t, info.name or "")
    if t == "hw_rds":
        # 华为 RDS 面板用 private_ip 做变量，name 作为补充
        return (t, info.serverinnerip or info.name or "")
    if t == "hw_ecs":
        return (t, info.serverinnerip or "")
    return (t, info.serverinnerip or info.name or "")


def _build_url(uid: str, variables: dict[str, str]) -> str:
    cfg = get_config()["grafana"]
    base = cfg["base"].rstrip("/")
    defaults: dict[str, str] = {
        "orgId": "1",
        "refresh": "1m",
        "from": cfg["default_from"],
        "to": cfg["default_to"],
        "var-interval": "1m",
        "var-datasource": "Prometheus",
    }
    # 用调用方传入的 variables 覆盖默认值，避免同名参数
    merged = {**defaults, **variables}
    return f"{base}/d/{uid}/?{urlencode(merged)}"


def _label_for(info: SchemaInfo) -> str:
    t = info.instance_type
    if t == "ali_rds":
        return f"阿里RDS {_ali_rds_prefix(info.serverinnerip)}"
    if t == "ali_polardb":
        return f"阿里PolarDB {info.name or _ali_rds_prefix(info.serverinnerip)}"
    if t == "ali_ecs":
        return f"阿里ECS {info.serverinnerip} ({info.name})"
    if t == "hw_taurusdb":
        return f"华为TaurusDB {info.name}"
    if t == "hw_rds":
        return f"华为RDS {info.serverinnerip} ({info.name})"
    if t == "hw_ecs":
        return f"华为ECS {info.serverinnerip} ({info.name})"
    return f"未知类型 {info.serverinnerip} / {info.name}"


def _url_for(info: SchemaInfo) -> str:
    cfg = get_config()["grafana"]
    uids = cfg["dashboards"]
    t = info.instance_type

    if t == "ali_rds":
        return _build_url(uids["ali_rds"], {
            "var-RegionId": "All",
            "var-Engine":   "All",
            "var-DBInstanceDescription": "All",
            "var-instanceId": _ali_rds_prefix(info.serverinnerip) or "All",
        })

    if t == "ali_polardb":
        return _build_url(uids["ali_polardb"], {
            "var-RegionId": "All",
            "var-DBType":   "MySQL",
            "var-DBClusterDescription": info.name or "All",
            "var-DBNodeRole": "Writer",
            "var-nodeId":   "All",
        })

    if t == "ali_ecs":
        return _build_url(uids["ali_ecs"], {
            "var-RegionId": "All",
            "var-Env":      "All",
            "var-Product_Line": "All",
            "var-Server_Type":  "All",
            "var-OSType":       "All",
            "var-PrivateIpAddress": info.serverinnerip or "All",
            "var-PublicIpAddress":  "All",
            "var-InstanceName":     "All",
            "var-instanceId":       "All",
        })

    if t == "hw_taurusdb":
        return _build_url(uids["hw_taurusdb"], {
            # 华为 TaurusDB 面板用 Prometheus-JVM 数据源
            "var-datasource": "Prometheus-JVM",
            "var-instance_name":       info.name or "All",
            "var-gaussdb_mysql_instance_id": "All",
            "var-node_name":           "All",
            "var-gaussdb_mysql_node_id": "All",
        })

    if t == "hw_rds":
        return _build_url(uids["hw_rds"], {
            "var-Env":          "All",
            "var-Product_Line": "All",
            "var-db_type":      "All",
            "var-private_ip":   info.serverinnerip or "All",
            "var-public_ip":    "All",
            "var-name":         info.name or "All",
            "var-instance_id":  "All",
        })

    if t == "hw_ecs":
        return _build_url(uids["hw_ecs"], {
            "var-Env":          "All",
            "var-Product_Line": "All",
            "var-Server_Type":  "All",
            "var-os_type":      "All",
            "var-private_ip_address": info.serverinnerip or "All",
            "var-public_ip_address":  "All",
            "var-name":         info.name or "All",
            "var-instance_id":  "All",
        })

    # 未知类型：给个首页兜底
    base = get_config()["grafana"]["base"].rstrip("/")
    return base


# ---------- 对外入口 ----------

def build_grafana_links(infos: list[SchemaInfo]) -> list[GrafanaLink]:
    """按"同一实例"聚合后返回链接列表。"""
    groups: dict[tuple[str, str], list[SchemaInfo]] = {}
    for info in infos:
        groups.setdefault(_instance_key(info), []).append(info)

    total_schemas = len(infos)
    links: list[GrafanaLink] = []
    for key, items in groups.items():
        rep = items[0]
        schemas = [i.schemaname for i in items]
        note = None
        if len(items) == total_schemas and total_schemas > 1:
            note = f"{total_schemas} 个业务库都在这个实例上"
        elif len(items) > 1:
            note = f"以下 {len(items)} 个库在这个实例上：{', '.join(schemas)}"
        links.append(GrafanaLink(
            url=_url_for(rep),
            instance_type=rep.instance_type,
            instance_label=_label_for(rep),
            schemas=schemas,
            note=note,
        ))
    return links
