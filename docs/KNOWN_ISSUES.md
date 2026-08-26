# V2.6 历史问题与 V3 修复记录

> 本文原本记录 V2.6 基线缺陷。V3 已修复重复拉取、mixed input 未解析、列表泄漏完整 URL、单文件架构、节点统计、并发刷新不合并、编辑先删 last-good 等核心问题。保留以下内容用于回归测试背景，不代表当前代码仍存在这些缺陷。

以下不是“可能优化”，而是交接时已经发现的真实工程问题。

## P0：刷新一个组的多个输出，会重复拉取上游

当前 `refresh_subscription()` 并行调用每个 output 的 `refresh_output()`；而每个 `refresh_output()` -> `convert_output()` -> `prepare_upstream_sources()`。

结果：如果一个组 4 个上游、5 个输出，理论上会产生约 20 次上游请求。

正确设计：

```text
refresh group
  ↓
fetch upstreams ONCE
  ↓
normalize/merge ONCE
  ↓
render outputs in parallel
```

必须重构。

---

## P0：完整 Clash/Mihomo YAML 与原始订阅混合兼容性未验证

用户真实上游包括 3x-ui `/clash/...` 链接，返回的是完整 Clash 配置，不一定是纯节点 URI 列表。

历史 V2.5 曾出现 subconverter HTTP 400；V2.6 改成先下载本地快照，但**尚未证明 subconverter 对 `url=/data/upstreams/file.body` 这种本地路径输入一定兼容**。

必须用真实/脱敏 fixtures 做集成测试，不能依赖猜测。

建议：

- 建立输入检测层；
- 对完整 Clash/Mihomo YAML 解析 `proxies`；
- 对原始订阅解码/解析为统一节点模型；
- 或使用一个明确支持 mixed input 的转换路径；
- 最终让 renderer 处理标准化节点集合，避免“把任何输入都扔给 subconverter”这种黑盒依赖。

如果继续使用 subconverter，也应明确它的 input contract，并用 HTTP 可访问的临时内部源，而不是未经验证的本地路径参数。

---

## P0：上游秘密的 API 暴露面仍需审计

当前列表 API `GET /api/subscriptions` 会直接返回 upstream `url`。

即使后台需要编辑，也建议：

- 列表接口默认只返回脱敏 URL/host；
- 详情编辑接口再返回完整 URL；
- 前端日志/错误永远只显示 host + 脱敏标识；
- 后端日志同样禁止打印完整 query/path token。

---

## P1：后端单文件过大

`app/main.py` 约 968 行，混合：

- DB migration
- auth
- settings
- subscription CRUD
- scheduler
- HTTP upstream fetch
- converter adapter
- cache
- public response

建议拆分：

```text
app/
  main.py
  config.py
  db.py
  models.py
  schemas.py
  security.py
  api/
    auth.py
    settings.py
    subscriptions.py
    public.py
  services/
    upstream_fetcher.py
    subscription_parser.py
    normalizer.py
    renderer.py
    cache.py
    scheduler.py
    converter.py
```

---

## P1：前端是单个 index.html

当前所有样式、DOM 和业务 JS 都塞在一个 `index.html`。这也是用户觉得 UI “粗糙”的重要原因。

应改为组件化前端，至少拆：

- Layout / Sidebar / Header
- Dashboard/GroupList
- SubscriptionGroupCard
- GroupEditor
- UpstreamTable
- OutputTable
- DiagnosticsDialog
- PublicUrlsDialog
- Settings
- Login
- Toast / Confirm

---

## P1：订阅节点统计尚未真正实现

UI 应展示标准化后的真实节点数、过滤掉的伪节点数、按上游来源计数，但当前没有稳定解析层，因此节点数不可可靠统计。

---

## 已处理：不聚合或透传套餐总流量

V3.1.1 起不再抓取、保存或向下游发送 `Subscription-Userinfo`。多个上游或节点的额度不同，聚合或任意选取一个上游都会产生错误语义。节点名称中明确携带的独立流量和重置时间只用于可选的智能改名。

---

## P1：scheduler 和同步 SQLite 在 async 上下文中较粗糙

单用户规模可以运行，但工程化版本建议：

- DB 访问抽象；
- 避免长事务；
- 对 refresh job 做明确状态机；
- 防止 scheduler 与客户端请求同时触发重复任务；
- 支持优雅退出。

---

## P1：默认 admin/admin 的安全体验

用户要求默认可以 `admin/admin`，要保留；但必须：

- 首次登录顶部强提示；
- 修改密码入口明显；
- 可选强制首次改密（如果不影响用户体验，可做设置）；
- 日志不能输出密码。

---

## P2：容器内两个进程的管理

当前 shell 后台启动 subconverter，然后 exec uvicorn。可以工作，但更稳的单容器实现可使用：

- supervisord / s6-overlay；或
- 后端自行管理子进程并 health check。

硬约束仍是单容器。

---

## P2：规则源稳定性

当前 ACL4SSR 远程规则可用，但需要：

- 失败 fallback；
- 明确 Mihomo/Clash 与 Surge/QuanX/Loon 的等价规则生成；
- 不要因为远程规则源短时失败导致整个订阅不可用。

---

## 已经符合方向的部分

这些需求方向是正确的，应保留：

- 单容器；
- 7777；
- SQLite + `/data`；
- 订阅组 -> 多上游 -> 多输出；
- 高熵公开 Token；
- Token reset；
- last-good；
- 定时刷新 + 请求过期刷新；
- Web 设置；
- 简化规则；
- `PROXY` 手选所有真实节点 + 自动选择 + DIRECT；
- 伪节点过滤。
