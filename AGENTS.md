# 仓库协作

- `app/` 是 FastAPI 控制面，`agent/` 是独立的节点运行 Agent，`frontend/` 是 Vue 管理界面。
- 用 Python 3.12 从 `requirements-dev.txt` 安装依赖，执行 `python -m pytest -q`。前端从锁文件安装依赖，执行 `pnpm test` 和 `pnpm build`。
- 修改订阅、数据库迁移或刷新状态时验证已有组、缓存、权限和跨任务一致性。内核相关变更在隔离环境验证，容器以 `/api/ready` 为就绪入口。
- 发版时同步根目录 `VERSION`、前端版本、Compose 默认值与变更记录。运行 `python scripts/check_release.py`，推送匹配的 `vX.Y.Z` 标签后由 Actions 发布多平台镜像。
