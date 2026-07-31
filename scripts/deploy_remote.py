# -*- coding: utf-8 -*-
"""
QwenPaw 远程自动部署脚本（在本地 Windows / 开发机运行）。

把"二开"后的代码自动上传到服务器、构建镜像、重建容器并校验控制台。
替代原先手工的 _ssh_upload.py / _ssh_run.py / _ssh_poll.py / _ssh_verify.py。

流程：
  1. 将本仓库（排除 .git / node_modules / 缓存 / 临时文件）打包为 tar.gz
  2. SFTP 上传到服务器 /data/qwenpaw
  3. 解压并触发服务端 deploy/upgrade.sh（setsid 后台执行，日志写入 build.log）
  4. 轮询 build.log，直到镜像构建与容器重建完成（UPGRADE_DONE）
  5. 校验 Web 控制台可访问（HTTP 200 / 容器 RUNNING）

用法：
  pip install paramiko
  python scripts/deploy_remote.py
  QWENPAW_DEPLOY_BACKUP=1 python scripts/deploy_remote.py   # 升级前额外备份数据卷

连接信息可用环境变量覆盖（避免把明文密码提交进仓库）：
  QWENPAW_DEPLOY_HOST / QWENPAW_DEPLOY_PORT / QWENPAW_DEPLOY_USER / QWENPAW_DEPLOY_PASS
"""
import os
import sys
import time
import tarfile

import paramiko

# ---- 连接配置（可用环境变量覆盖；不要把明文密码提交到仓库）----
HOST = os.environ.get("QWENPAW_DEPLOY_HOST", "10.56.5.140")
PORT = int(os.environ.get("QWENPAW_DEPLOY_PORT", "22"))
USER = os.environ.get("QWENPAW_DEPLOY_USER", "root")
PASS = os.environ.get("QWENPAW_DEPLOY_PASS", "Git@bjbus%")

REMOTE_DIR = "/data/qwenpaw"
REMOTE_TAR = "/data/qwenpaw/qwenpaw_upload.tar.gz"
COMPOSE_FILE = os.environ.get("QWENPAW_COMPOSE_FILE", "deploy/docker-compose.prod.yml")
DO_BACKUP = os.environ.get("QWENPAW_DEPLOY_BACKUP", "0")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 打包时排除的目录 / 后缀 / 文件
EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                "dist", "build", ".idea", ".vscode", "generated-images", ".pytest_cache"}
EXCLUDE_EXT = {".pyc", ".pyo", ".egg-info", ".tar.gz", ".log", ".zip"}
EXCLUDE_FILES = {"qwenpaw_upload.tar.gz", "build.log",
                 "_ssh_upload.py", "_ssh_run.py", "_ssh_poll.py", "_ssh_verify.py"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def make_archive():
    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "qwenpaw_upload.tar.gz")
    print(f"[pack] creating {tmp}")
    n = 0
    with tarfile.open(tmp, "w:gz") as tar:
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                if f in EXCLUDE_FILES or any(f.endswith(e) for e in EXCLUDE_EXT):
                    continue
                full = os.path.join(root, f)
                arc = os.path.relpath(full, REPO_ROOT)
                tar.add(full, arcname=arc)
                n += 1
    size = os.path.getsize(tmp)
    print(f"[pack] {n} files -> {size} bytes")
    return tmp


def ssh_cmd(ssh, cmd, timeout=60):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return out, err, rc


def main():
    tar_local = make_archive()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30)
    try:
        sftp = ssh.open_sftp()
        try:
            sftp.remove(REMOTE_TAR)
        except IOError:
            pass
        print(f"[upload] -> {REMOTE_TAR}")
        sftp.put(tar_local, REMOTE_TAR)
        sftp.close()

        print("[upload] extracting on server ...")
        o, e, c = ssh_cmd(ssh, f"cd {REMOTE_DIR} && tar xzf qwenpaw_upload.tar.gz "
                                f"&& rm -f qwenpaw_upload.tar.gz && echo OK")
        print(o.strip(), e.strip())

        print("[deploy] triggering server-side upgrade (detached) ...")
        trigger = (
            f"cd {REMOTE_DIR} && QWENPAW_COMPOSE_FILE={COMPOSE_FILE} "
            f"QWENPAW_DEPLOY_BACKUP={DO_BACKUP} "
            f"setsid bash deploy/upgrade.sh > build.log 2>&1 < /dev/null & echo started"
        )
        o, e, c = ssh_cmd(ssh, trigger, timeout=30)
        print("trigger:", o.strip(), e.strip())

        print("[deploy] polling build.log ...")
        done, failed = False, False
        for i in range(90):               # 90 * 20s ≈ 30 分钟上限
            time.sleep(20)
            o, e, c = ssh_cmd(ssh, "tail -n 8 /data/qwenpaw/build.log", timeout=30)
            line = o.strip().replace("\n", " | ")
            print(f"[poll {i}] {line}")
            if "UPGRADE_DONE" in o:
                done = True
                break
            if "BUILD_FAIL" in o:
                failed = True
                break

        if failed:
            print("[deploy] FAILED: 构建/升级失败，请登录服务器查看 build.log")
        elif not done:
            print("[deploy] WARNING: 超时未检测到 UPGRADE_DONE，请登录服务器查看 build.log")

        print("[verify] HTTP check ...")
        o, e, c = ssh_cmd(ssh,
            "curl -s -o /dev/null -w 'HTTP_STATUS=%{http_code}\\n' --max-time 10 http://127.0.0.1:8088/ ; "
            "docker ps --filter name=qwenpaw --format '{{.Names}} {{.Status}}'", timeout=30)
        print(o.strip(), e.strip())
    finally:
        ssh.close()
    print("DONE")


if __name__ == "__main__":
    main()
