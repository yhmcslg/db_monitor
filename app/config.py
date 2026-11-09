"""配置加载：读 config.yaml，提供 get_config() 单例。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@lru_cache(maxsize=1)
def get_config() -> dict:
    path = Path(os.environ.get("DB_MONITOR_CONFIG", DEFAULT_PATH))
    with path.open("r", encoding="utf-8") as fp:
        return yaml.safe_load(fp)


def derive_schemas(tenant_code: str) -> list[str]:
    """根据租户代码推出 4 个业务库名。"""
    cfg = get_config()
    return [tpl.format(code=tenant_code) for tpl in cfg["schemas"]]


def schema_label(schema: str, tenant_code: str) -> str:
    """把 schema 名反查成业务名：geely -> 考勤 / schedule_geely -> 排班 等。"""
    cfg = get_config()
    labels = cfg.get("schema_labels") or {}
    for tpl, label in labels.items():
        if tpl.format(code=tenant_code) == schema:
            return label
    return schema  # 兜底，映射不到就显示原 schema 名


def format_schemas(schemas: list[str], tenant_code: str) -> list[str]:
    """把 schema 列表全部转换成业务名。"""
    return [schema_label(s, tenant_code) for s in schemas]
