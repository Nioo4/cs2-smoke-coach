# SmokeLine

面向中文 CS2 玩家的 Mirage 烟雾教学检索工具，以及开发中的阶段式动态战术板。

## 功能与架构

- 文字/语音输入：从本地已发布目录查找教学路线；支持同时提供起点与目标、只给目标、只给起点、泛化查询。
- React + TypeScript + Vite 前端，FastAPI 后端，PostgreSQL 与 Alembic 迁移，Nginx + Docker Compose 部署。
- 确定性目录匹配优先，受约束的模型意图提取与语音转写按配置启用；模型不能凭空生成教学路线。外部视频搜索不包含在当前版本中。
- 管理员维护路线、主视频和目标地图位置。教学素材不随源码发布。
- `collaboration/` 为独立本地原型：Node.js、WebSocket、Yjs、PostgreSQL，支持多人文字协作、地图动作及阶段式动画演示。

## 本地运行教学应用

安装 Docker Desktop（Linux 容器模式）后，在仓库根目录执行：

```powershell
docker compose --env-file .env.example -p smokeline-dev -f compose.yml -f compose.dev.yml up -d --build postgres backend nginx
```

访问 http://127.0.0.1:8080 。公开示例配置默认关闭 AI 供应商；没有附带生产数据库、用户账号或教学视频。语音和模型能力需要自行配置合法的服务凭据，切勿提交真实 `.env`。

开发配置的数据库口令仅供本机开发使用，不适用于公网。生产使用前必须配置独立强口令、管理员密码、TLS、访问控制与备份。

停止服务但保留数据：

```powershell
docker compose --env-file .env.example -p smokeline-dev -f compose.yml -f compose.dev.yml stop
```

## 动态战术板

运行方法和双参与者本地测试票据说明见 [collaboration/README.md](collaboration/README.md)。入口 `/stage-board.html` 支持阶段编排、角色移动、道具目标、文字说明、自动演示和逐阶段演示。

该模块仍是本地协作机制原型，不代表正式个人账号、队伍权限、公开分享或完整验收已经完成。示例动画只表达编排，不模拟真实游戏弹道或实战耗时。

## 目录

| 目录 | 用途 |
| --- | --- |
| `frontend/` | 页面、组件及前端测试 |
| `backend/` | API、检索、媒体处理、迁移及后端测试 |
| `collaboration/` | 动态战术板与协作原型 |
| `deploy/` | Nginx 和运维配置模板 |
| `scripts/` | 验证及运维辅助脚本 |
| `evals/` | 检索评测用例 |

## 发布边界

此仓库为当前工作版本的公开源码快照，不包含旧 Git 历史。内部施工文档、部署地址记录、验收截图、对话记录、生产数据、密钥、密码和原始素材未公开。部分内部测试说明可能引用未公开的施工文档；这不代表相关验收已对外复现。

本项目非 Valve 官方产品。游戏及地图相关权利归各自权利人所有；公开展示源码不等于为第三方素材授予再分发许可。本仓库尚未指定开源许可证。
