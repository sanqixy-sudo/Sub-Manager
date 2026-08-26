# 当前 V2.6 原型架构

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
