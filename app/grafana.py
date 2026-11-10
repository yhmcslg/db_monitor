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
        # 调阿里云 Open API 查该集群的 Writer 节点 ID；查不到就用 All
        node_id = "All"
        if info.name:
            try:
                from .aliyun import get_polardb_writer_node_id
                node_id = get_polardb_writer_node_id(info.name) or "All"
            except Exception:  # noqa: BLE001
                pass
        return _build_url(uids["ali_polardb"], {
            "var-RegionId": "All",
            "var-DBType":   "MySQL",
            "var-DBClusterDescription": info.name or "All",
            "var-DBNodeRole": "Writer",
            "var-nodeId":   node_id,
        })

    if t == "ali_ecs":
        # 注意：不要传 var-instanceId=All，否则 Grafana 会卡在级联解析里；
        # 不传这个参数让面板走默认值，即可立即出数据
        return _build_url(uids["ali_ecs"], {
            "var-RegionId": "All",
            "var-Env":      "All",
            "var-Product_Line": "All",
            "var-Server_Type":  "All",
            "var-OSType":       "All",
            "var-PrivateIpAddress": info.serverinnerip or "All",
            "var-PublicIpAddress":  "All",
            "var-InstanceName":     "All",
        })

    if t == "hw_taurusdb":
        # 节点名约定为 <实例名>_node01
        node_name = f"{info.name}_node01" if info.name else "All"
        # 调华为云 Open API 查 Master 节点 ID（Grafana var-gaussdb_mysql_node_id 必须具体值）
        node_id = "All"
        if info.name:
            try:
                from .huaweicloud import get_taurusdb_master_node_id
                node_id = get_taurusdb_master_node_id(info.name) or "All"
            except Exception:  # noqa: BLE001
                pass
        return _build_url(uids["hw_taurusdb"], {
            "var-datasource": "Prometheus-JVM",
            "var-instance_name":       info.name or "All",
            "var-gaussdb_mysql_instance_id": "All",
            "var-node_name":           node_name,
            "var-gaussdb_mysql_node_id": node_id,
        })

    if t == "hw_rds":
        # 不传 var-instance_id=All，避免 Grafana 级联解析导致 No data
        return _build_url(uids["hw_rds"], {
            "var-Env":          "All",
            "var-Product_Line": "All",
            "var-db_type":      "All",
            "var-private_ip":   info.serverinnerip or "All",
            "var-public_ip":    "All",
            "var-name":         info.name or "All",
        })

    if t == "hw_ecs":
        # 注意：不要传 var-instance_id=All，原因同阿里 ECS
        return _build_url(uids["hw_ecs"], {
            "var-Env":          "All",
            "var-Product_Line": "All",
            "var-Server_Type":  "All",
            "var-os_type":      "All",
            "var-private_ip_address": info.serverinnerip or "All",
            "var-public_ip_address":  "All",
            "var-name":         info.name or "All",
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
