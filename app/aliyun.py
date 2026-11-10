"""阿里云 Open API 包装：目前只用来查 PolarDB Writer 节点 ID。

接口：
    get_polardb_writer_node_id(cluster_description: str) -> str | None
        返回 Writer 节点的 DBNodeId（形如 pi-uf6xxx）；查不到或未配置 AK 返回 None。

缓存 1 小时，避免每张图都调用 Open API。
"""
from __future__ import annotations

import threading
import time

from .config import get_config


_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()
_TTL = 3600


def _creds() -> tuple[str, str, str] | None:
    """读 config.yaml，返回 (ak, sk, region) 或 None（未配置）。"""
    cfg = get_config().get("aliyun") or {}
    ak = (cfg.get("access_key_id") or "").strip()
    sk = (cfg.get("access_key_secret") or "").strip()
    region = (cfg.get("region_id") or "cn-shanghai").strip()
    if not ak or not sk:
        return None
    return ak, sk, region


def _client():
    """延迟导入 SDK，没装/没配就返回 None。"""
    creds = _creds()
    if creds is None:
        return None
    ak, sk, region = creds
    try:
        from alibabacloud_polardb20170801.client import Client
        from alibabacloud_tea_openapi.models import Config
    except ImportError:
        return None
    config = Config(
        access_key_id=ak,
        access_key_secret=sk,
        region_id=region,
        endpoint=f"polardb.{region}.aliyuncs.com",
    )
    return Client(config)


def get_ecs_instance_id(private_ip: str) -> str | None:
    """用 ECS 内网 IP 反查 instanceId（形如 i-uf6xxx），供 Grafana var-instanceId 用。"""
    if not private_ip:
        return None

    now = time.time()
    with _cache_lock:
        hit = _cache.get(f"ecs:{private_ip}")
        if hit and now - hit[0] < _TTL:
            return hit[1]

    creds = _creds()
    if creds is None:
        return None
    ak, sk, region = creds

    try:
        from alibabacloud_ecs20140526.client import Client
        from alibabacloud_ecs20140526 import models as ecs_models
        from alibabacloud_tea_openapi.models import Config
    except ImportError:
        return None

    try:
        client = Client(Config(
            access_key_id=ak,
            access_key_secret=sk,
            region_id=region,
            endpoint=f"ecs.{region}.aliyuncs.com",
        ))
        req = ecs_models.DescribeInstancesRequest(
            region_id=region,
            private_ip_addresses='["' + private_ip + '"]',
        )
        resp = client.describe_instances(req)
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[aliyun] DescribeInstances failed for '{private_ip}': {exc}", file=sys.stderr)
        return None

    instances = ((resp.body.instances.instance if resp and resp.body and resp.body.instances else None) or [])
    if not instances:
        import sys
        print(f"[aliyun] No ECS matches private ip '{private_ip}' (check region_id)", file=sys.stderr)
        return None

    iid = instances[0].instance_id
    if not iid:
        return None
    with _cache_lock:
        _cache[f"ecs:{private_ip}"] = (now, iid)
    import sys
    print(f"[aliyun] ECS ip '{private_ip}' -> instanceId={iid}", file=sys.stderr)
    return iid


def get_polardb_writer_node_id(cluster_description: str) -> str | None:
    """用集群描述（Grafana 面板里的 DBClusterDescription）查 Writer 节点 ID。"""
    if not cluster_description:
        return None

    now = time.time()
    with _cache_lock:
        hit = _cache.get(cluster_description)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    client = _client()
    if client is None:
        return None

    try:
        from alibabacloud_polardb20170801 import models as polar_models
    except ImportError:
        return None

    # 1. 用 DBClusterDescription 搜集群，拿 DBClusterId
    try:
        req = polar_models.DescribeDBClustersRequest(
            region_id=_creds()[2],
            dbcluster_description=cluster_description,
        )
        resp = client.describe_dbclusters(req)
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[aliyun] DescribeDBClusters failed for '{cluster_description}': {exc}", file=sys.stderr)
        return None

    items = (resp.body.items.dbcluster
             if resp and resp.body and resp.body.items else None) or []
    if not items:
        import sys
        print(f"[aliyun] No cluster matches description '{cluster_description}'", file=sys.stderr)
        return None

    # 精确匹配优先，拿不到就取第一个
    exact = [c for c in items if (c.dbcluster_description or "") == cluster_description]
    target = (exact or items)[0]
    cluster_id = target.dbcluster_id

    # 2. 用 DBClusterId 查节点列表
    try:
        req2 = polar_models.DescribeDBClusterAttributeRequest(dbcluster_id=cluster_id)
        resp2 = client.describe_dbcluster_attribute(req2)
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[aliyun] DescribeDBClusterAttribute failed for '{cluster_id}': {exc}", file=sys.stderr)
        return None

    nodes = (resp2.body.dbnodes if resp2 and resp2.body else None) or []
    writer = None
    for n in nodes:
        role = getattr(n, "dbnode_role", None) or getattr(n, "db_node_role", None)
        if role and role.lower() == "writer":
            writer = getattr(n, "dbnode_id", None) or getattr(n, "db_node_id", None)
            break

    if not writer:
        import sys
        print(f"[aliyun] Writer node not found in cluster '{cluster_id}'", file=sys.stderr)
        return None

    with _cache_lock:
        _cache[cluster_description] = (now, writer)
    import sys
    print(f"[aliyun] '{cluster_description}' -> cluster={cluster_id}, writer={writer}", file=sys.stderr)
    return writer
