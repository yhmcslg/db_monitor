"""华为云 Open API 包装：查 GaussDB(for MySQL)/TaurusDB 的 Master 节点 ID。

接口：
    get_taurusdb_master_node_id(instance_name: str) -> str | None

调用链：
    ListGaussMySqlInstances(name=xxx)   -> 拿实例 id
    ShowGaussMySqlInstanceInfo(id)      -> 从 nodes 里找 type=master 的 id

缓存 1 小时。
"""

import logging as _logging
_logger = _logging.getLogger("db_monitor")

def _log(msg: str) -> None:
    try:
        _logger.info(msg)
    except Exception:
        pass

from __future__ import annotations

import threading
import time

from .config import get_config


_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()
_TTL = 3600


def _creds() -> tuple[str, str, str, str] | None:
    cfg = get_config().get("huaweicloud") or {}
    ak = (cfg.get("access_key_id") or "").strip()
    sk = (cfg.get("access_key_secret") or "").strip()
    region = (cfg.get("region_id") or "cn-east-3").strip()
    project_id = (cfg.get("project_id") or "").strip()
    if not ak or not sk or not project_id:
        return None
    return ak, sk, region, project_id


def _client():
    creds = _creds()
    if creds is None:
        return None
    ak, sk, region, project_id = creds
    try:
        from huaweicloudsdkcore.auth.credentials import BasicCredentials
        from huaweicloudsdkcore.http.http_config import HttpConfig
        from huaweicloudsdkgaussdb.v3.gaussdb_client import GaussDBClient
        from huaweicloudsdkgaussdb.v3.region.gaussdb_region import GaussDBRegion
    except ImportError:
        return None

    credentials = BasicCredentials(ak, sk, project_id)
    return (GaussDBClient.new_builder()
            .with_credentials(credentials)
            .with_region(GaussDBRegion.value_of(region))
            .with_http_config(HttpConfig.get_default_config())
            .build())


def get_ecs_instance_id(name: str | None = None, private_ip: str | None = None) -> str | None:
    """用 ECS 名字或内网 IP 反查实例 ID，供 Grafana var-instance_id 用。"""
    if not name and not private_ip:
        return None

    cache_key = f"ecs:{name or ''}|{private_ip or ''}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    creds = _creds()
    if creds is None:
        return None
    ak, sk, region, project_id = creds

    try:
        from huaweicloudsdkcore.auth.credentials import BasicCredentials
        from huaweicloudsdkcore.http.http_config import HttpConfig
        try:
            from huaweicloudsdkecs.v2 import EcsClient
        except ImportError:
            from huaweicloudsdkecs.v2.ecs_client import EcsClient
        from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion
        from huaweicloudsdkecs.v2.model.list_servers_details_request import \
            ListServersDetailsRequest
    except ImportError:
        return None

    try:
        client = (EcsClient.new_builder()
                  .with_credentials(BasicCredentials(ak, sk, project_id))
                  .with_region(EcsRegion.value_of(region))
                  .with_http_config(HttpConfig.get_default_config())
                  .build())
        req = ListServersDetailsRequest(
            name=name or None,
            ip=private_ip or None,
        )
        resp = client.list_servers_details(req)
    except Exception as exc:  # noqa: BLE001
        import sys
        _log(f"[huaweicloud] ListServersDetails failed (name={name}, ip={private_ip}): {exc}")
        return None

    servers = (resp.servers if resp else None) or []
    if not servers:
        import sys
        _log(f"[huaweicloud] No ECS matches name='{name}' ip='{private_ip}' (check region_id)")
        return None

    sid = getattr(servers[0], "id", None)
    if not sid:
        return None
    with _cache_lock:
        _cache[cache_key] = (now, sid)
    import sys
    _log(f"[huaweicloud] ECS (name={name}, ip={private_ip}) -> id={sid}")
    return sid


def get_taurusdb_master_node_id(instance_name: str) -> str | None:
    if not instance_name:
        return None

    now = time.time()
    with _cache_lock:
        hit = _cache.get(instance_name)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    client = _client()
    if client is None:
        return None

    try:
        from huaweicloudsdkgaussdb.v3.model.list_gauss_my_sql_instances_request import \
            ListGaussMySqlInstancesRequest
        from huaweicloudsdkgaussdb.v3.model.show_gauss_my_sql_instance_info_request import \
            ShowGaussMySqlInstanceInfoRequest
    except ImportError:
        return None

    # 1) 按名字查实例
    try:
        req = ListGaussMySqlInstancesRequest(name=instance_name)
        resp = client.list_gauss_my_sql_instances(req)
    except Exception as exc:  # noqa: BLE001
        import sys
        _log(f"[huaweicloud] ListGaussMySqlInstances failed for '{instance_name}': {exc}")
        return None

    instances = (resp.instances if resp else None) or []
    exact = [i for i in instances if (getattr(i, "name", None) or "") == instance_name]
    target = (exact or instances)[0] if (exact or instances) else None
    if target is None:
        import sys
        _log(f"[huaweicloud] No instance matches '{instance_name}'")
        return None

    instance_id = getattr(target, "id", None)
    if not instance_id:
        return None

    # 2) 查实例详情，拿节点列表
    try:
        req2 = ShowGaussMySqlInstanceInfoRequest(instance_id=instance_id)
        resp2 = client.show_gauss_my_sql_instance_info(req2)
    except Exception as exc:  # noqa: BLE001
        import sys
        _log(f"[huaweicloud] ShowGaussMySqlInstanceInfo failed for '{instance_id}': {exc}")
        return None

    detail = resp2.instance if resp2 else None
    nodes = (detail.nodes if detail else None) or []
    master_id = None
    for n in nodes:
        # MysqlInstanceNodeInfo 字段：id, name, type, status, ...
        # type 一般是 master / slave / readnode
        ntype = (getattr(n, "type", None) or "").lower()
        if ntype == "master":
            master_id = getattr(n, "id", None)
            break

    if not master_id:
        import sys
        # 没 master 时打出节点 type 分布，方便 debug
        types = [getattr(n, "type", None) for n in nodes]
        _log(f"[huaweicloud] Master not found in '{instance_name}', node types={types}")
        return None

    with _cache_lock:
        _cache[instance_name] = (now, master_id)
    import sys
    _log(f"[huaweicloud] '{instance_name}' -> instance_id={instance_id}, master={master_id}")
    return master_id
