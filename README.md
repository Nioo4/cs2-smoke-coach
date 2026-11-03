# SmokeLine / CS2 Workshop

面向中文 CS2 玩家的烟雾教学与战术演示工具：输入想封的位置，查找已收录的教学视频；在地图上编排角色、动作与阶段，动态演示一套战术。

- **烟雾教学**：支持起点＋目标、只说目标、只说起点等查询，查看路线说明和视频。目前以 Mirage 为主。
- **动态战术板**：编排阶段、角色站位、移动路线、道具目标和文字说明，支持自动演示、逐阶段演示与多人协作。
- **内容管理**：管理员维护教学路线、主视频和地图目标位置。
- **技术栈**：React、TypeScript、FastAPI、PostgreSQL；战术协作使用 Node.js、WebSocket 和 Yjs。

当前公开版本的战术板是独立本地原型，使用临时参与票据进入，不包含正式个人账号、队伍权限和公开分享。动画用于说明战术流程，不模拟真实游戏弹道。

## 准备环境

安装 Git、Docker Desktop（Linux 容器模式）。运行战术板还需要 Node.js 24（含 npm）。以下命令使用 Windows PowerShell。

```powershell
git clone https://github.com/Nioo4/cs2-smoke-coach.git
cd cs2-smoke-coach
```

## 启动烟雾教学

在仓库根目录执行；如果已有自己的 `.env`，保留它，不要重复复制覆盖。

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -p smokeline-dev -f compose.yml -f compose.dev.yml up -d --build postgres backend nginx
```

首次启动需要下载镜像、安装依赖和初始化数据库。完成后打开 [教学首页](http://127.0.0.1:8080)。

### 如何使用

1. 输入“我在 Mirage 匪家，想封警家烟”或“警家烟怎么丢”。
2. 查看匹配的起点与目标，打开教学并手动播放视频。
3. 也可以从地图选择目标位置，再查看对应路线。

仓库不附带教学视频和生产数据，首次启动可能没有可播放路线，需要自行录入素材。默认关闭模型与语音服务；文字目录检索可独立使用。语音和模型意图解析需要在 `.env` 中配置供应商凭据后重建后端。当前版本不提供外部视频平台搜索。

### 录入教学素材

生成管理员密码配置，按提示输入密码：

```powershell
docker compose --env-file .env -p smokeline-dev -f compose.yml -f compose.dev.yml run --rm --no-deps backend python -m app.scripts.create_admin_hash
```

把输出的 `ADMIN_PASSWORD_HASH_B64=...` 替换到 `.env` 的同名配置，保留 `ADMIN_USERNAME=admin`，然后运行：

```powershell
docker compose --env-file .env -p smokeline-dev -f compose.yml -f compose.dev.yml up -d backend
```

打开 [内容管理](http://127.0.0.1:8080/admin)，使用 `admin` 和刚设置的密码登录。创建起点与目标、填写路线、上传一个主视频，待处理完成后发布；同一起点与目标可以设置默认路线。

停止服务并保留数据：

```powershell
docker compose --env-file .env -p smokeline-dev -f compose.yml -f compose.dev.yml stop
```

## 启动动态战术板

从仓库根目录进入 `collaboration`，执行：

```powershell
cd collaboration
npm ci
docker compose -f compose.t0.yml up -d --wait postgres
npm run build
$env:TACTICS_T0_MODE = 'local'
$env:DATABASE_URL = 'postgresql://t0_test:t0_only_local@127.0.0.1:55432/t0_tactics'
npm start
```

保持终端运行，打开 [动态战术板](http://127.0.0.1:4173/stage-board.html)。另开一个 PowerShell 窗口，获取本地参与票据：

```powershell
$tickets = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:4173/__t0/test-tickets -Headers @{ Origin = 'http://127.0.0.1:4173' } -ContentType 'application/json' -Body '{"document_id":"mirage-stage-prototype"}'
$tickets.participants
```

### 如何使用

1. 把输出中的一份参与者票据填入页面并连接。票据五分钟内有效、只能使用一次；失效后重新获取。
2. 创建或选择阶段，添加角色，在地图上设置站位。
3. 添加移动、等待、观察、道具或文字动作，在地图上标记位置，填写说明，设置接续或同时执行。
4. 使用自动演示播放整套流程，或逐阶段演示并讲解；阶段不要求填写精确时间戳。
5. 测试多人协作时，在另一个标签页打开同一地址，使用另一份票据连接，观察彼此的修改。
6. 等待页面显示已保存后离开。遇到未确认修改时，先导出副本，再按页面提示处理；导出不等于保存成功。

停止战术板：在运行 `npm start` 的窗口按 `Ctrl+C`，再在 `collaboration` 目录执行：

```powershell
docker compose -f compose.t0.yml stop postgres
```

以上为本机运行方式，数据卷会保留。示例数据库口令仅供本地使用；不要公开 `.env`、管理员密码或参与票据。项目非 Valve 官方产品，使用视频及地图素材时请确保具备相应使用权限。
