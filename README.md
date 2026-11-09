# db_monitor

按租户代码一键拼出 Grafana 监控链接与 ELK 慢 SQL 短链。

## 工作流程

1. 输入租户代码（如 `geely`），推导 4 个业务库：
   - `geely`（考勤）、`schedule_geely`（排班）、`corehr_geely`（新人事）、`gaia_acc_geely`（累计）
2. 连 `devops.info` 表查这 4 个库对应的服务器信息
3. 按 `datacenter` + `instance_type` 两列识别实例类型并拼 Grafana URL（大小写不敏感）：
   | datacenter | instance_type | 对应面板 | 传入变量 |
   | --- | --- | --- | --- |
   | ALi | ECS | 阿里云 ECS 监控 | `PrivateIpAddress=<serverinnerip>` |
   | ALi | RDS | 阿里云 RDS 监控 | `instanceId=<serverinnerip 第一个点之前的前缀>` |
   | ALi | PolarDB | 阿里云 PolarDB MySQL 监控 | `DBClusterDescription=<name>` |
   | huawei | ECS | 华为云 ECS 监控 | `private_ip_address=<serverinnerip>`、`name` |
   | huawei | RDS | 华为云 RDS 监控 | `private_ip=<serverinnerip>`、`name` |
   | huawei | TaurusDB | 华为云 GaussDB(for MySQL) 监控 | `instance_name=<name>` |
4. 4 个库若落在同一实例，合并成一条链接并标注"4 个业务库都在这个实例上"
5. 用 4 个用户名拼 Kibana discover URL；若配置了 Kibana 账号密码，再调 `POST /api/short_url` 换成 `goto/` 短链

## 目录

```
db_monitor/
├── config.yaml          # 配置：MySQL/Grafana/Kibana/Server
├── requirements.txt
├── run.py               # 启动入口
├── app/
│   ├── config.py        # 配置加载
│   ├── db.py            # MySQL 查询 + 类型识别
│   ├── grafana.py       # Grafana URL 构造 + 同实例聚合
│   ├── elk.py           # ELK discover URL + 短链 API
│   ├── screenshot.py    # Playwright 登录 Grafana + 单 panel 截图 + Pillow 拼接
│   └── main.py          # FastAPI 路由
├── templates/
│   └── index.html       # 首页表单
└── tests/
    └── smoke.py         # 冒烟测试（不连 MySQL/ELK）
```

## 安装与启动

```bash
pip install -r requirements.txt
playwright install chromium   # Grafana 截图用，首次 ~200MB
python run.py                 # 默认 0.0.0.0:8000
```

然后：

- 浏览器打开 `http://<host>:8000/`，输入租户代码点查询
- 或者 `curl 'http://<host>:8000/api/lookup?tenant=geely'` 拿 JSON

## 配置要点（config.yaml）

- `mysql.*`：连 `10.1.50.10:3306/devops`，账号密码写在这里
- `mysql.query_sql`：`{schemas}` 占位符会被替换成 4 个带引号的库名
- `kibana.username` / `kibana.password`：配了才会调短链 API；不配就返回长链
- `grafana.default_from` / `kibana.default_from`：默认 `now-1h`
- `grafana.username` / `grafana.password`：Grafana 截图功能用，Playwright 自动登录
- `grafana.panels`：每种面板里 CPU / 内存 / 磁盘IO 等 panelId 的映射，顺序即截图拼接顺序
- `screenshot.*`：开关、panel 宽高、超时、cookie 文件、内存缓存 TTL
- `schemas`：租户代码到 4 个库名的模板，`{code}` 是占位符

## Grafana 截图

页面每个实例卡片下会异步加载一张 PNG：顶部实例标签 + 库列表，下面纵向排列 CPU / 内存 / 磁盘 IO 等 panel，每个 panel 显示最近 1 小时走势。

实现方式：Playwright 打开 Grafana 的 `/d-solo/<uid>?panelId=N&from=now-1h&to=now&...`（单 panel 全屏视图），逐张截图后用 Pillow 竖向拼接。

需要：
- `playwright install chromium` 装好浏览器
- `config.yaml` 填好 Grafana 账号密码和 `grafana.panels`
- 部署机能访问 Grafana

想临时关掉截图：把 `config.yaml` 里 `screenshot.enabled` 设为 `false`，页面会直接只显示链接。

## 冒烟测试

不连任何外部依赖：

```bash
python tests/smoke.py
```

应输出 "全部冒烟通过 ✅"。
# db_monitor
