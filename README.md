# CS2 Workshop

面向中文玩家的 CS2 Mirage 烟雾教学与战术协作工具。

- 烟雾教学：按目标、起点检索已收录路线，地图点选，查看步骤与主视频。
- 个人战术本：创建战术，编排阶段、角色与地图动作，查看历史、归档与恢复。
- 队伍协作：共享战术本、成员角色、邀请与实时协作。
- 演示与分享：演示已保存版本，发布固定版本的只读分享；允许复制时可建立独立副本。
- 账户与内容管理：个人资料、密码与恢复码；教学后台独立认证，管理点位、路线和媒体。

## 本地启动

需要 Git、Docker Desktop 的 Linux 容器环境及 Docker Compose v2。Docker 构建会下载基础镜像、npm 与 Python 依赖，需要可用网络；完整容器启动不要求宿主机另装 Node/Python。

首次获取项目：

```powershell
git clone https://github.com/Nioo4/cs2-smoke-coach.git
cd cs2-smoke-coach
```

在仓库根目录执行以下 PowerShell 命令：

```powershell
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml build backend collaboration
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml build --build-arg BUILD_MODE=production nginx
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml up -d --no-build
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml ps
```

打开 [CS2 Workshop 本地页面](http://127.0.0.1:18220)。等待数据库、后端和网页服务就绪；可检查：

```powershell
Invoke-WebRequest http://127.0.0.1:18220/api/v1/readyz
```

这是只监听本机的专用配置，数据库密码、服务内部通信密钥和后台哈希均是公开本地夹具，不能用于公网。后端使用 `APP_ENV=test`，前端以上述命令构建为 production 模式；这不等于生产部署。不要将此文件与 `compose.yml` 或生产配置合并。

配置固定使用18220端口、`smokeline-tactics-t5-local-*`命名卷/网络及10.244.98.0/24、10.244.99.0/24子网。确认它们未被已有实例占用；仅改Compose项目名不能隔离这些固定资源。已有同配置服务时不要再次进行“全新初始化”。

## 使用

1. 进入“账户”注册自己的用户名、昵称及12–128字符密码。保存首次显示的恢复码，确认保存后继续；恢复码不会再次显示。
2. 在“个人战术本”创建战术，打开编辑器，配置阶段、角色和动作。留意保存与连接状态；演示入口读取已确认版本。
3. 在“我的队伍”创建队伍、邀请成员并按需要分配权限。新加入成员默认只读；队伍成员与地图角色是不同概念。
4. 在具体战术中打开发布分享管理。分享读取固定发布版本，草稿后续修改不会自动覆盖；更新发布和撤销需要明确操作。
5. 教学首页只返回自己目录中已发布的教学。首次空数据库没有预置教学或示例视频；要建立教学目录，使用独立教学后台配置自己的点位、路线和授权视频。

本地默认关闭模型与语音识别供应商：明确名称检索按真实目录处理，模糊意图和录音转写不保证可用，不会用假结果补齐。录音需浏览器麦克风授权。此启动配置不包含外部供应商接入步骤。

### 可选：设置自己的教学管理员密码

普通战术账号自行注册，不需要教学管理员密码。需要使用 `/admin` 时，可在完成镜像构建后生成自己的密码哈希：

```powershell
docker run --rm -it --network none --read-only --tmpfs /tmp smokeline-backend:tactics-t5-local python -m app.scripts.create_admin_hash
```

在隐藏提示中输入并确认密码。输出的 `ADMIN_PASSWORD_HASH_B64` 值只保存在自己控制的本地配置，不发布。创建被Git忽略的 `.workcache/admin.local.yml`，填写自己的非秘密管理员名和生成的哈希：

```yaml
services:
  backend:
    environment:
      ADMIN_USERNAME: local-admin
      ADMIN_PASSWORD_HASH_B64: "替换为刚生成的哈希值，不含变量名及等号"
```

然后只重新创建后端，保留数据库与媒体：

```powershell
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml -f .workcache/admin.local.yml up -d --no-build --no-deps backend
```

后台使用上述管理员名与自己输入的密码登录；它与战术账号独立。公开配置内的夹具哈希不提供可用的个人密码说明。不要将真实密码、私有覆盖文件或恢复码提交到Git。

## 停止与再次启动

停止服务，保留数据：

```powershell
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml stop
```

已有卷初始化后，按依赖顺序启动四个常驻服务，跳过一次性 `spool-init`，避免对非空临时卷重复执行首次初始化：

```powershell
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml up -d --no-build --no-deps --wait postgres
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml up -d --no-build --no-deps --wait backend
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml up -d --no-build --no-deps --wait collaboration
docker compose --env-file deploy/tactics.empty.env -f compose.tactics.local.yml up -d --no-build --no-deps --wait nginx
```

若使用了管理员私有覆盖文件，以上每条命令均在基础 `-f` 后追加 `-f .workcache/admin.local.yml`，避免后端配置被基础夹具替换。不要用 `down -v` 作为重启方式，它会删除数据卷。
