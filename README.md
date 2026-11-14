# 🚀 RemnaShop-Pro

当前版本：`V3.9.0`

RemnaShop-Pro 是一个面向 **Remnawave 面板** 的 Telegram 机器人，提供订阅售卖、续费、状态查询与基础运维能力。

当前适配目标为 **Remnawave Panel 3.4.8**，对应官方
`@remnawave/backend-contract 3.4.15`。接口来源、完整调用清单和旧数据升级规则
分别见 [契约来源](docs/remnawave-contract-source.md)、
[完整 OpenAPI 差异](docs/remnawave-openapi-diff.md)、
[API 清单](docs/remnawave-api-inventory.md)、
[3.x 迁移说明](docs/remnawave-v3-migration.md) 和
[V3.7 RC 验收步骤](docs/v3.7-rc-runbook.md)、
[人工 UI 清单](docs/v3.7-rc-ui-checklist.md) 和
[V3.8 发布说明](docs/releases/v3.8.md) 和
[V3.8.1 发布说明](docs/releases/v3.8.1.md) 和
[V3.8.2 发布说明](docs/releases/v3.8.2.md) 和
[V3.9.0 支付接入说明](docs/releases/v3.9.0.md)。

已核对 2026-11-08 的官方最新稳定版：Panel 3.4.8 源码仍使用契约 3.4.15。
官方 3.4.8 OpenAPI 与此前 3.4.4 的路径、参数和 Schema 相同；仓库已更新为
实际下载的 3.4.8 契约，并加入版本与校验值锁定。详见
[3.4.8 兼容性与升级验收](docs/remnawave-3.4.8-compatibility.md)。
所有 Panel 写请求均禁止 HTTP 层自动重试，避免响应丢失后重复开通、续期或重置；
读取仍保留有界重试，写入结果不确定时须先人工核对。

---

## 功能

### 用户端
- 购买新订阅 / 续费：人民币定价，ZPAY 支付宝扫码，确认到账后自动交付
- HTTPS 收银台、支付状态查询与漏通知补查；USDT、人工审核新购入口暂时隐藏
- 我的订阅 / 续费
- 订阅详情查看（到期时间、状态、流量使用）
- 订阅链接二维码生成
- 查看和清理本人 HWID 设备、重置订阅凭据；重置后展示面板返回的最新链接和二维码
- 已有订阅续费用官方 Extend Action；结果不确定时订单进入人工核对状态，禁止重复执行
- 节点状态查询
- 联系客服

### 管理端
- 套餐管理（新增、查看、删除）
- 用户列表与订阅管理（查看、删除、重置流量、重置策略）
- 在线支付订单与交付状态查看；保留历史人工订单审核（通过 / 拒绝）
- 批量续期、批量撤销订阅（选定数值用户 ID、执行前确认）
- HWID 设备管理与统计、节点 GeoCheck、系统 Dashboard
- 面板用户 ID / shortUuid / 用户名 Resolve 检索和 Panel Tags 查看
- 到期提醒天数设置
- 过期清理天数设置
- 异常检测阈值与检测周期设置

---

## 部署要求

> 本仓库 **仅支持 Docker Compose 部署**，不再支持 systemd / 纯 Python / 服务器裸装方式。

- Linux 服务器（推荐 Debian / Ubuntu）
- 网络可访问 GitHub 与 Docker 镜像仓库
- 以可提权用户执行（root 或具备 sudo 权限）
- 生产镜像使用 Python 3.11.16

---

## 一键安装（唯一推荐方式）

```bash
curl -fsSL https://raw.githubusercontent.com/ike-sh/RemnaShop-Pro/main/bootstrap.sh | bash
```

该命令会执行仓库内 `bootstrap.sh`，自动完成：

1. 检查并自动安装通用基础依赖（缺失时）：`curl`、`ca-certificates`、`git`、`bash`、`tar`、`gzip`、`unzip`、`jq`、`sed`、`grep`、`awk`、`coreutils`
2. 检查并安装 Docker（缺失时）
3. 检查并安装 Docker Compose 插件（缺失时）
4. 克隆/更新仓库到 `/opt/remnashop-pro`
5. 若 `.env` 不存在则基于 `.env.example` 自动创建
6. 使用中文交互收集必填 `ADMIN_ID` 与 `BOT_TOKEN`（已有值可选择保留或替换），并自动写入 `.env`
7. 启动 Docker Compose 栈
8. 等待 `remnashop` 与 `http` 两个容器均达到 `healthy` 后才报告安装成功

已有安装通过同一个 `bootstrap.sh install` 入口安全更新。若安装目录非空但不是
已验证的项目仓库，脚本会停止并保留目录内容。自定义 `INSTALL_DIR` 时，目录名仍须为
`remnashop-pro` 或 `RemnaShop-Pro`。

示例交互（节选）：

```text
[remnashop-bootstrap] 正在配置必填环境变量（仅 ADMIN_ID 与 BOT_TOKEN）。
请输入 ADMIN_ID:
请输入 BOT_TOKEN:
```

> `bootstrap.sh` 支持两种模式：
> - 交互菜单（直接执行 `bash bootstrap.sh`）
> - 非交互参数：`bash bootstrap.sh install` / `bash bootstrap.sh uninstall`
> - 仅准备配置和构建、不启动：`bash bootstrap.sh install --no-start`。此时可暂不填写 Bot / 支付参数，不代表运行验收通过。
> - 已校验的本地源码可附加 `--local-source`，避免拉取覆盖；origin 必须与 `REPO_URL` 一致。详见 [支付部署说明](docs/zpay-payments.md#先部署稍后填写配置不启动)。

---

## 卸载（仅移除 RemnaShop-Pro 资源）

### 非交互卸载（服务器本地脚本）

```bash
cd /opt/remnashop-pro
bash bootstrap.sh uninstall
```

### 远程一行卸载（不经过菜单）

```bash
curl -fsSL https://raw.githubusercontent.com/ike-sh/RemnaShop-Pro/main/bootstrap.sh | bash -s -- uninstall
```

卸载会**仅**清理以下 RemnaShop-Pro 资源：
- Compose 项目 `remnashop`
- 该项目创建的容器
- 该项目创建的本地镜像（`--rmi local`）
- 该项目创建的卷（`-v`）
- 项目目录 `/opt/remnashop-pro`

不会触碰其他 Compose 项目或无关 Docker 资源。

> 卸载安全行为：
> - 检测到交互终端时，必须手动确认才会执行删除（支持 `YES` / `yes` / `Y` / `y`）；
> - 非交互场景会跳过确认，但仅在安装目录通过仓库校验后处理该项目资源；
> - 若安装目录不存在，脚本不删除任何 Docker 资源；若目录存在但校验不通过，脚本报错退出。

---

## 安装成功判定与失败排查

安装脚本的**成功判定**不是 `docker compose up -d` 返回成功，而是：

- `remnashop` 与 `http` 两个容器均实际进入 `health=healthy` 状态。

若出现以下情况会直接判定安装失败并给出日志排查提示：
- 容器退出（`exited/dead`）
- 健康检查 `unhealthy`
- 容器持续重启（`restarting` 或重启次数异常增长）
- 在超时时间内未进入 `healthy`

手动复核命令：

```bash
cd /opt/remnashop-pro
docker --version
docker compose version
test -f .env && echo ".env exists"
docker compose -p remnashop ps
docker compose logs --tail=100 remnashop http
```

预期结果：
- `docker --version` 能输出版本号
- `docker compose version` 能输出版本号
- `.env exists` 输出成功
- `docker compose -p remnashop ps` 显示 `remnashop`、`http` 两个服务为 `running` 且健康检查最终为 `healthy`
- 日志中无持续崩溃重启

Bot 健康检查验证本地配置与 SQLite 完整性；HTTP 健康检查验证 HTTP 应答及支付账本可访问。
它们不验证外部 Panel、Telegram、支付网关连通性或公网 HTTPS 回调可达性。
外部集成状态需另行验证，不能把 healthy 当作支付上线验收。

---

## 支付宝支付与通用 HTTP 服务

本版增加独立 HTTP 服务，可挂载其他商品模块路由；共享支付账本按商品类型关联业务订单，
当前实现订阅的自动开通／续费消费者。没有开放匿名传金额下单接口。

在服务器 `.env` 配置 `ZPAY_PID`、`ZPAY_KEY`、`PAYMENT_PUBLIC_URL`、可选的 `ZPAY_CID`，
并配置 `HTTP_TRUSTED_PROXIES`。真实密钥不要提交 Git 或发送给机器人。

- 宿主机入口默认 `127.0.0.1:8080`，交给已有 HTTPS 反代。
- 异步通知地址：`https://你的域名/payments/zpay/notify`。
- 新订单采用浏览器 POST 跳转到 ZPAY `submit.php`，不再调用 `mapi.php` 生成二维码；二维码由 ZPAY 收银台展示。
- 支付返回地址：`https://你的域名/payments/zpay/return`；页面跳回不是到账依据。
- 管理员可在“订单审计 → 订单详情”二次确认删除未支付订单（列表移除、审计保留）；晚到的可信到账通知会恢复订单，已付款/已交付不可删除。
- 支付前固化人民币金额和套餐快照；重复回调只记账一次，Panel 写入不盲目重放。
- 所有支付参数全空时可以安装，但支付入口不可用；部分支付配置不完整时 HTTP 服务拒绝启动。

**完整配置、Nginx 示例、可信代理设置、支付异常处理与其他商品扩展见
[支付接入文档](docs/zpay-payments.md)。** 本地源码未提交前，远程默认安装命令不会包含这些改动；
当前 bootstrap 默认仓库仍是上游 `ike-sh/RemnaShop-Pro`，使用 Fork 时必须明确指定匹配的
`REPO_URL` 与已发布分支，不能把本地修改当成已经发布的版本。

---

## 日常运维（Docker Compose）

```bash
cd /opt/remnashop-pro
./docker-manage.sh ps
./docker-manage.sh logs
./docker-manage.sh restart
./docker-manage.sh down
```

`docker-manage.sh up/update` 均调用唯一的安装入口 `bootstrap.sh install`。
升级生产实例前请备份 `remnashop-data` 数据卷；备份中包含 Secret 和用户数据。

### V3.6 → V3.7 升级前备份

先在旧实例上停止本项目服务，再将数据卷复制到**项目目录之外**的新目录。
以下命令使用旧版已有的 Python 镜像，不依赖尚未安装的 V3.7 备份工具。
服务停止期间不得运行其他写入该数据库的实例；任一步失败时停止后续升级：

```bash
cd /opt/remnashop-pro
set -e
docker volume inspect remnashop_remnashop-data >/dev/null
docker compose -p remnashop stop remnashop
test ! -e /opt/remnashop-backups/pre-v3.7
mkdir -p -m 700 /opt/remnashop-backups/pre-v3.7
docker run --rm -v remnashop_remnashop-data:/from:ro -v /opt/remnashop-backups/pre-v3.7:/to --entrypoint sh remnashop-remnashop:latest -c 'test -f /from/config.json && test -f /from/starlight.db && cp -a /from/. /to/'
docker run --rm -v /opt/remnashop-backups/pre-v3.7:/snapshot --entrypoint python remnashop-remnashop -c "import json,sqlite3; from pathlib import Path; p=Path('/snapshot'); assert (p/'config.json').is_file() and (p/'starlight.db').is_file(); c=json.loads((p/'config.json').read_text()); db=sqlite3.connect(p/'starlight.db'); assert c.get('admin_id') and c.get('bot_token') and db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; db.close(); print('backup verified')"
```

只有校验通过后才继续升级。若暂不升级，使用
`docker compose -p remnashop start remnashop` 恢复原服务。V3.7 的
`docker-manage.sh backup` 会要求服务已停止、备份目录为空且位于仓库外，并自动
验证配置文件与 SQLite 完整性。再次备份需指定另一个空目录。

恢复备份会覆盖项目数据，且只能在所有数据库写入服务停止后执行（新版含 HTTP）：

```bash
cd /opt/remnashop-pro
docker compose -p remnashop stop remnashop http
REMNASHOP_RESTORE_CONFIRM=YES BACKUP_DIR=/opt/remnashop-backups/pre-v3.7 ./docker-manage.sh restore
docker compose -p remnashop start remnashop http
docker compose -p remnashop ps
```

不要将备份放在 `/opt/remnashop-pro` 内；项目卸载会删除该目录。恢复后仍需检查
容器健康与旧订阅迁移状态。

---

## 环境变量说明

首次安装会自动从 `.env.example` 生成 `.env`（若不存在），并在安装过程中交互收集并写入以下必填项：

- `ADMIN_ID`
- `BOT_TOKEN`

`.env.example` 中这两个字段默认是空值（`ADMIN_ID=`、`BOT_TOKEN=`），用于确保首次安装必须由管理员输入真实值。

若 `.env` 已存在，脚本会询问你“保留还是替换”，但不会显示现有 `BOT_TOKEN` 内容，
输入新 Token 时也不会在终端回显。
若 `.env` 来自模板且 `ADMIN_ID` / `BOT_TOKEN` 为空，脚本会直接要求输入新值，不会询问“是否保留空值”。
脚本会校验 `ADMIN_ID` 与 `BOT_TOKEN` 的基本格式。交互输入使用 `/dev/tty`；
无 TTY 时，只有 `.env` 已有有效必填值才能继续。

以下变量在安装阶段均为可选，不会阻塞部署；可后续在机器人/应用内配置：

- `PANEL_URL`
- `PANEL_TOKEN`
- `SUB_DOMAIN`
- `GROUP_UUID`
- `PANEL_VERIFY_TLS`

面板地址和 Token 未配置时，面板功能不可用。新套餐在机器人「套餐管理」中从面板
全部内部组列表多选 UUID，无需填写全局 `GROUP_UUID`；该变量仅作为旧套餐的单组兼容默认值。
安装容器本身不要求这些可选值。

### 套餐组合多个内部组

- 新建套餐：名称 → 人民币价格 → 天数 → 流量 → 重置策略 → **内部组多选** → 保存。
- 机器人通过正式 `GET /api/internal-squads` 读取所有组，显示组名与 UUID；支持分页、取消选中和刷新，至少选择一个组。
- 已有套餐：套餐管理 → 套餐详情 → **选择内部组**，可修改组合；不同套餐可以绑定不同或重叠的组。
- 新购将所选 UUID 数组写入用户 `activeInternalSquads`；续费按购买时套餐快照更新组权限（替换为该套餐的组，不是与旧权限取并集）。
- 下单后修改套餐不影响已创建订单；交付前若快照中的组被删除，订单停止交付并保留到账记录，需管理员核对，不自动替换成其他组。
- 旧套餐未选择专属组时仍使用 `GROUP_UUID` 新购，旧订单/旧套餐续费保持原有组权限，不强制迁移。
- 修改套餐不会主动批量调整已开通用户；新设置仅作用于之后创建的订单。

实现和验收边界见 [套餐内部组组合](docs/plan-internal-groups.md)。

已有 `config.json` 时，`.env` 中非空的 `ADMIN_ID`、`BOT_TOKEN`、
`PANEL_TOKEN` 在容器启动时优先。空 `PANEL_TOKEN` 允许管理员在机器人中配置；
其他可选面板参数仅在首次生成配置时由环境变量初始化，随后保留后台修改。

## 生产镜像构建说明

生产镜像通过 `.dockerignore` 排除非运行时文件，不会打包以下内容：

- `tests/`
- `docs/`
- `README.md`
- `AGENTS.md`
- `LICENSE`
- `.gitignore`
- `.env.example`

---

## 迁移说明（旧版 standalone / server-install 用户）

旧版 `install.sh + systemd(remnashop.service)` 部署流已移除。

迁移步骤：

1. 备份旧机器中的 `config.json` 与 `starlight.db`
2. 执行新的一键安装命令（见上）
3. 将备份数据恢复到 Docker 数据卷（或容器 `/data`）
4. 使用 `docker compose ps` 与日志确认服务正常

> 若旧机器仍在运行 `remnashop.service`，请先停用旧服务再切换，避免重复实例同时运行。

### 从 Remnawave 2.x 升级到 3.4.8

升级前备份 `remnashop-data` 数据卷。旧 SQLite 中的用户 UUID 会完整保留；
新版以数值用户 ID 调用面板。只有 Telegram ID 对应关系唯一时才自动绑定；
其他记录显示为“待迁移”或“有歧义”，需要管理员按
[迁移说明](docs/remnawave-v3-migration.md)核对绑定。未绑定的旧订阅不会被
自动重建、续费或删除。旧版本机器人无法继续调用 3.x 已移除的 UUID 路由，
不要把回滚机器人代码当作可直接恢复兼容性的办法。

---

## 项目结构

- `bot.py`：主程序入口
- `docker-compose.yml`：唯一部署编排入口
- `bootstrap.sh`：一键安装引导脚本（用于 curl | bash）
- `docker-manage.sh`：Docker Compose 运维助手
- `handlers/`：消息与回调处理辅助代码
- `services/`：面板 API、通用 HTTP、ZPAY 支付与订单服务代码
- `storage/`：数据库初始化、通用支付账本与访问辅助代码
- `jobs/`：定时任务辅助代码
- `utils/`：通用工具函数

---

## 联系

- 作者：ike
- 群组：https://t.me/Remnawarecn

---

本项目仅供学习交流使用，请遵守当地法律法规。
