<p align="center">
  <img src="frontend/public/brand-logo.png" width="132" alt="Sub Manager Logo">
</p>

<h1 align="center">Sub Manager</h1>

<p align="center">
  面向个人与小团队的自托管代理订阅运维中心<br>
  统一管理订阅、手动节点、改名分类、多客户端输出与真实节点测活
</p>

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/version-v3.3.2-2563eb?style=flat-square">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-single--container-2496ed?style=flat-square&logo=docker&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="Vue" src="https://img.shields.io/badge/Vue-3-42b883?style=flat-square&logo=vuedotjs&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5-3178c6?style=flat-square&logo=typescript&logoColor=white">
  <img alt="Mihomo" src="https://img.shields.io/badge/Mihomo-1.19.30-f97316?style=flat-square">
</p>

---

Sub Manager 将多个远程订阅和手动节点汇总为稳定的公开订阅地址。系统对上游只拉取一次，完成解析、过滤、去重、改名和分类后，再生成各类客户端输出；刷新失败时保留 last-good，不让短暂故障直接影响下游。

## 主要功能

- **统一节点来源**：支持 Clash/Mihomo YAML、3x-ui、Base64、URI 列表和单节点；可建立完全不含远程订阅的手动节点组。
- **安全手动节点**：支持 VLESS、VMess、Trojan、SS、SSR、Hysteria2、TUIC、AnyTLS 等协议，连接信息使用 AES-256-GCM 加密保存。
- **三级改名**：组默认规则、单上游覆盖规则、单节点别名依次生效；流量和剩余天数变化不会让节点身份或分类失效。
- **自定义分类**：来源整组归类和单节点精确分配可以混用，分类可排序；Mihomo 的 `PROXY` 入口直接包含分类、自动选择、`DIRECT` 和真实节点。
- **真实节点测活**：通过节点分别请求 Cloudflare 与 Google 的 HTTPS 204 地址，展示延迟、可用性、连续失败和 30 天历史；测活结果不会自动删除节点。
- **多客户端输出**：提供 Mihomo、Clash、Surge、Quantumult X、Loon、Surfboard、V2Ray、SS/SSR 等固定公开地址。
- **可靠刷新**：singleflight 合并定时、手动和公共请求触发的并发刷新；逐上游快照和原子 last-good 保证可恢复性。
- **运维后台**：包含概览、订阅组、节点测活、运行记录、检查工具、系统设置和完整组详情，兼容桌面与 390px 手机界面。
- **安全边界**：HttpOnly 管理会话、Argon2id、Origin 校验、集中脱敏、响应大小与并发限制；管理列表不返回完整秘密。

> Sub Manager 不聚合或透传套餐流量 Header，也不会根据测活结果自动过滤节点。默认规则保持“中国大陆直连，其他流量使用 `PROXY`”。

## 工作流程

```text
远程订阅 / 加密手动节点
            │
            ▼
  拉取 → 解析 → 过滤 → 去重
            │
            ▼
     改名 → 分类 → 节点快照
            │
            ├────────► HTTPS 双目标测活与历史
            │
            ▼
   多客户端渲染 → 固定公开订阅地址
```

## 快速部署

### Docker Compose

```bash
git clone https://github.com/sanqixy-sudo/Sub-Manager.git
cd Sub-Manager
docker compose up -d --build
```

管理后台：`http://服务器地址:7777`

首次登录使用初始管理员账号 `admin` / `admin`，登录后请立即修改账号和密码。生产环境建议在前方配置 HTTPS 反向代理，并设置正确的公开访问域名。

### Docker CLI

```bash
docker build -t sub-manager:v3.3.2 .

docker run -d \
  --name sub-manager \
  --restart unless-stopped \
  -p 7777:7777 \
  -e TZ=Asia/Shanghai \
  -v /opt/sub-manager/data:/data \
  sub-manager:v3.3.2
```

容器只暴露 `7777`，数据库、缓存、密钥和输出均位于 `/data`。重建或升级容器时必须保留该目录。

## 升级与备份

升级前先备份完整 `/data`，再构建新镜像并原位替换容器。数据库迁移会在启动时自动执行，并在 schema 升级前创建备份。旧版本不能直接写入已经升级的数据库，回滚时请同时恢复升级前的数据备份。

详细步骤见 [部署文档](docs/DEPLOYMENT.md) 和 [升级指南](UPDATE_GUIDE.md)。

## 技术栈

- 后端：FastAPI、Pydantic、HTTPX、SQLite
- 前端：Vue 3、TypeScript、Vite、Pinia、Element Plus
- 渲染与测活：subconverter、Mihomo v1.19.30
- 进程管理：supervisord
- 部署：单 Docker 容器，支持 amd64 / arm64

## 本地开发

### 后端

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

### 前端

```bash
cd frontend
npm install
npm test
npm run build
```

## 项目结构

```text
app/                    FastAPI 后端
frontend/               Vue 3 管理台与品牌资源
tests/                  后端回归测试
replacements/config/    固定规则模板
docs/                   架构、安全、部署与验收文档
Dockerfile              生产镜像
compose.yaml            单容器部署示例
```

## 安全

请勿提交 `/data`、数据库、密钥、真实订阅、代理 URI 或带 Token 的公开地址。若发现安全问题，请不要在公开 Issue 中粘贴秘密内容，处理建议见 [安全说明](docs/SECURITY.md)。

## 许可证

本项目目前未附带开源许可证。在许可证正式加入前，默认保留所有权利；公开可见不等于授权复制、修改或再分发。
