# 当前架构：V3.5

主控使用 FastAPI + SQLite V12，前端为 Vue 3 / TypeScript / Vite / Element Plus。supervisord 管理主控和 subconverter；Mihomo 按需承担测活。主控维持单容器、7777 和 /data。

订阅链路：app/api → repository / services/fetcher → parser → renamer / categories → renderer → 原子 last-good。刷新按组 singleflight 合并；Mihomo 和 V2Ray 使用原生输出路径，其余兼容格式经内部临时源交给 subconverter。

代理服务链路：app/control/api.py → service.py → schema.py / secrets.py。管理服务器、TLS 节点、链式出站、用户、多套餐实例及独立授权。Agent 通过单独认证的 /api/agent 接口拉取目标配置、回传实际配置版本和累计用量。agent/control_agent.py 在节点服务器上管理带用户统计的 sing-box，agent/stats.py 读取仅本机可达的 gRPC 计数器。

用户门户 /#/portal 使用独立会话；个人 /p/{token}/{client_type} 输出原生 Mihomo 或 sing-box 配置。原有 /s/{token}/{slug} 公开订阅不变。安装、配额语义、升级与当前验证范围见 [代理服务说明](PROXY_CONTROL.md)。

## 以下为 V2.6 原型历史存档

以下结构和缺陷描述保留用于理解交接背景，不代表当前实现。

## 技术栈

- 后端：FastAPI
- 数据库：SQLite
- HTTP：httpx
- 转换器：tindy2013/subconverter（同一容器内子进程）
- 前端：单文件 `app/static/index.html`，原生 HTML/CSS/JS
- 进程启动：`entrypoint.sh`
- 对外端口：7777
- 持久化：`/data`

## 当前文件

```text
Dockerfile
entrypoint.sh
requirements.txt
app/
  main.py                # 约 968 行，绝大多数后端逻辑都在这里
  static/index.html      # UI/CSS/JS 全在单文件
replacements/
  config/submanager.ini  # 固定规则模板
```

## 当前数据库表

### subscriptions

- id
- name
- token
- interval_minutes
- enabled
- last_refresh_at
- last_refresh_status
- last_error
- last_attempt_at
- last_success_at
- refresh_count
- note
- created_at
- updated_at

### upstreams

- id
- subscription_id
- name
- url
- enabled
- sort_order
- created_at
- updated_at

### outputs

- id
- subscription_id
- client_type
- name
- slug
- update_interval_minutes
- enabled
- created_at
- updated_at

### settings

key/value 配置存储。

## 当前主要 API

```text
POST   /api/login
GET    /api/meta
GET    /api/settings
PUT    /api/settings
GET    /api/client-types
GET    /api/subscriptions
POST   /api/subscriptions
PUT    /api/subscriptions/{id}
DELETE /api/subscriptions/{id}
POST   /api/subscriptions/{id}/diagnose
POST   /api/subscriptions/{id}/refresh
POST   /api/subscriptions/{id}/rotate-token
GET    /api/health
GET    /s/{token}/{slug}
```

## 当前刷新链路

V2.6 逻辑：

```text
订阅组刷新
  ↓
对每个 output 并行 refresh_output
  ↓
每个 output 又会调用 prepare_upstream_sources
  ↓
每个 output 都重新并发拉所有 upstream
  ↓
写入 /data/upstreams 快照
  ↓
把本地快照路径通过 url=path1|path2 传给 subconverter
  ↓
写入 /data/cache/{token}_{slug}.*
```

这个链路存在重大问题，见 `KNOWN_ISSUES.md`。

## 当前规则

`replacements/config/submanager.ini`：

- LAN -> DIRECT
- ChinaDomain -> DIRECT
- ChinaCompanyIp -> DIRECT
- GEOIP CN -> DIRECT
- FINAL -> PROXY
- `PROXY = select(自动选择, DIRECT, 全部节点)`
- `♻️ 自动选择 = url-test(全部节点)`

方向符合用户要求，可继续优化规则源和各客户端等价渲染。
