# Sub Manager V3 更新说明

本更新包用于从 V2.6 或任意 V3 版本升级到 V3.3.0，也可用于全新部署。

## 升级前

1. 备份当前宿主机上的持久化目录，例如 `/opt/sub-manager/data`。
2. 确认旧容器实际挂载的是同一个目录，并记录现有端口和反向代理配置。
3. 不要删除或清空 `/data`。V3.3.0 首次启动会自动将 SQLite 迁移到 V7，并在 `/data/backups` 创建升级前备份。

## 构建镜像

在解压后的目录执行：

```bash
docker build -t sub-manager:v3.3.0 .
```

## 替换旧容器

以下示例保留原数据目录与公开端口：

```bash
docker stop sub-manager
docker rm sub-manager

docker run -d \
  --name sub-manager \
  --restart unless-stopped \
  -p 127.0.0.1:7777:7777 \
  -e TZ='Asia/Shanghai' \
  -v /opt/sub-manager/data:/data \
  sub-manager:v3.3.0
```

如果原服务需要直接通过服务器 IP 访问，可将端口参数改为 `-p 7777:7777`。

## 验证

```bash
curl -fsS http://127.0.0.1:7777/api/health
docker logs --tail 100 sub-manager
```

然后登录管理后台，确认：

- 原订阅组、Token 和公开 `/s/{token}/{slug}` 地址仍存在；
- 上游与输出配置完整；
- 手动刷新能正常生成目标客户端输出；
- 默认账号仍为 `admin / admin` 时，立即在系统设置中修改。
- 概览能显示 scheduler 状态，运行记录页能记录手动、定时和公共请求刷新；
- “节点测活”页显示 Mihomo v1.19.30，手动测试一个真实组可得到 Cloudflare/Google 两项结果；
- 设置页可配置自动测活周期、并发和超时，测试目标不可修改；
- 编辑组可导入手动 URI/YAML，保存后页面与 API 均不回显连接参数；
- 分类管理可勾选整条来源或单个远程/手动节点，刷新后 Mihomo `PROXY` 可看到分类入口；
- 浏览器直接打开公开订阅显示文本，访问同一地址并添加 `?download=1` 才下载；
- VLESS Reality 组的 V2Ray/Shadowrocket 输出不再出现 `No nodes were found`。
- 订阅组成功刷新后，完整详情的节点页能看到不含连接凭据的名称快照。
- 已有组升级后的首次成功刷新会建立节点基线；后续新增或固定名称变化节点会显示 ⚠️ 和待确认提示。
- 登录页用户名和密码保持空白，不显示默认账号提示；默认凭据仅在登录后安全警告中提醒修改。

## 回滚

停止 V3.3 容器，完整恢复升级前的 `/data` 备份，再使用 V3.2.1 原镜像启动。V3.2.1 不能直接写入已经迁移并使用过的 V7 数据库。
