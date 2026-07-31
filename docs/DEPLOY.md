# QwenPaw 远程自动部署指南

把本地二开后的代码自动上传到服务器、构建镜像、重建容器并校验控制台。
整套流程由 **2 个文件** 驱动，取代了早期手工的 `_ssh_upload.py` / `_ssh_run.py` / `_ssh_poll.py` / `_ssh_verify.py`。

## 文件清单

| 文件 | 角色 | 运行位置 |
|------|------|----------|
| `scripts/deploy_remote.py` | 本地编排：打包 → 上传 → 触发升级 → 轮询 → 校验 | 本地（Windows/开发机） |
| `deploy/upgrade.sh` | 服务端执行：构建镜像 → 重建容器（保留卷） | 服务器 |
| `deploy/docker-compose.prod.yml` | 生产 compose：复用既有 3 个外部卷 | 服务器 |
| `scripts/docker_build.sh` | 构建镜像（已内置 `DOCKER_BUILDKIT=1`） | 服务器 |

## 前置条件

- 本地安装 `paramiko`：`pip install paramiko`
- 服务器 Docker 已启用 **BuildKit**（`docker_build.sh` 已强制 `DOCKER_BUILDKIT=1`）
- 服务器目录 `/data/qwenpaw` 存在，且三个命名卷已存在：
  `qwenpaw-data` / `qwenpaw-secrets` / `qwenpaw-backups`
- SSH 可达（默认 `10.56.5.140:22`，账号密码见 `deploy_remote.py` 顶部，建议用环境变量覆盖）

## 日常升级（二开完成后一条命令）

```powershell
pip install paramiko
python scripts/deploy_remote.py
```

带升级前数据卷备份（推荐在重大改动前）：

```powershell
$env:QWENPAW_DEPLOY_BACKUP=1
python scripts/deploy_remote.py
```

脚本会依次：
1. **打包**：把仓库（排除 `.git` / `node_modules` / 缓存 / 临时文件）打成 `tar.gz`
2. **上传**：SFTP 到 `/data/qwenpaw/qwenpaw_upload.tar.gz` 并解压
3. **规范化**：把可能带入的 CRLF 文本文件转回 LF（避免容器内脚本启动失败）
4. **触发升级**：`setsid bash deploy/upgrade.sh > build.log 2>&1 < /dev/null &`（后台脱离会话，不阻塞）
5. **轮询** `build.log`，直到出现 `UPGRADE_DONE`（或 `BUILD_FAIL`）
6. **校验**：`curl` 探活 `127.0.0.1:8088` 并检查容器状态

## 服务端升级内部做了什么（`deploy/upgrade.sh`）

1. 防御性把文本文件 CRLF→LF
2. （可选）用 `busybox` 把三个数据卷各打一个 `tar.gz` 存到 `/data/qwenpaw/volumes-bak/<时间戳>/`
3. `DOCKER_BUILDKIT=1 bash scripts/docker_build.sh qwenpaw:latest` 构建镜像
4. `docker rm -f qwenpaw` → `docker compose -f deploy/docker-compose.prod.yml up -d`
   - **只删容器，三个数据卷原封不动** → V2 代码自动迁移 V1 旧格式（`_migrate_legacy_*`），无需手工 migrate

## 首次部署到新服务器（或换机）

```bash
# 1) 建目录与三个卷
mkdir -p /data/qwenpaw
docker volume create qwenpaw-data
docker volume create qwenpaw-secrets
docker volume create qwenpaw-backups

# 2) 本地先跑一次 deploy_remote.py 把代码传上去并构建
python scripts/deploy_remote.py

# 3) 浏览器打开 http://<服务器IP>:8088 核对记忆/通道/技能
```

> 全新环境无 `config.json` 时，容器 `entrypoint.sh` 会自动 `qwenpaw init --defaults` 初始化。
> 已有数据卷则可被直接接管（见上）。

## 回滚预案

三个数据卷永不删除，旧镜像 `agentscope/qwenpaw:latest` 也保留，随时可复活旧服务：

```bash
docker stop qwenpaw && docker rm qwenpaw
docker run -d --name qwenpaw --restart always -p 8088:8088 \
  -v qwenpaw-data:/app/working \
  -v qwenpaw-secrets:/app/working.secret \
  -v qwenpaw-backups:/app/working.backups \
  agentscope/qwenpaw:latest
```

若升级前开了 `QWENPAW_DEPLOY_BACKUP=1`，还可从 `/data/qwenpaw/volumes-bak/<时间戳>/` 还原卷内容。

## 安全建议

- 当前 `docker-compose.prod.yml` 未开启认证（与历史部署一致）。若服务暴露公网，
  取消注释其中的 `QWENPAW_AUTH_ENABLED=true` 并设置用户名密码。
- `deploy_remote.py` 内含明文密码默认值；提交仓库前请用环境变量
  `QWENPAW_DEPLOY_PASS` 注入，或将其加入 `.gitignore`。

## 已知坑（已内置处理，仅作记录）

- **CRLF**：Windows 上传的 `.sh` 带 `\r` 会导致 bash 路径解析错乱、`entrypoint.sh` 启动失败。
  `upgrade.sh` 在构建前对所有文本文件做 `sed -i 's/\r$//'`。
- **BuildKit**：`Dockerfile` 用了 BuildKit 专属的 `COPY --chmod=755`，legacy builder 会卡死。
  已通过 `docker_build.sh` 的 `export DOCKER_BUILDKIT=1` 解决。
- **SSH 后台阻塞**：用 `setsid ... > build.log 2>&1 < /dev/null &` 彻底脱离通道，避免本地脚本卡死。
