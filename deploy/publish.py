#!/usr/bin/env python3
"""只从 learning-web 的 main 发布三个静态文件，不执行仓库里的代码。"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone


SOURCE = Path("/srv/projects/learning-web")
WEBSITE = Path("/srv/www/learning-web")
STATE = Path("/var/lib/learning-web-deploy")
REPOSITORY = "https://github.com/Lyh-1011/learning-web.git"
DOMAIN = "learn.shenzhen-test.cloud"
FILES = ("index.html", "style.css", "script.js")


def run(*args, timeout=40):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        args, check=True, capture_output=True, timeout=timeout, env=environment
    ).stdout


def git(*args):
    return run("git", "-c", "core.hooksPath=/dev/null", "-C", str(SOURCE), *args)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def replace_file(path, content, mode=0o644):
    # 临时文件和目标放在同一目录，最后一次替换不会产生半写的文件。
    descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_served_files(contents):
    for name, expected in contents.items():
        route = "/" if name == "index.html" else "/" + name
        actual = run(
            "curl", "--fail", "--silent", "--show-error",
            "--connect-timeout", "5", "--max-time", "10",
            "--resolve", f"{DOMAIN}:443:127.0.0.1", f"https://{DOMAIN}{route}",
            timeout=15,
        )
        if actual != expected:
            raise RuntimeError(f"HTTPS 返回的 {name} 与待发布版本不一致")


def publish():
    if SOURCE.is_symlink() or WEBSITE.is_symlink():
        raise RuntimeError("项目目录已变化，请先由维护者核对")
    if git("remote", "get-url", "origin").decode().strip() != REPOSITORY:
        raise RuntimeError("服务器仓库地址与学习项目不一致")
    if git("branch", "--show-current").decode().strip() != "main":
        raise RuntimeError("服务器仓库没有处于 main 分支")
    if git("status", "--porcelain"):
        raise RuntimeError("服务器源码有本地改动，停止发布以保留这些改动")

    git("fetch", "--quiet", "origin", "main")
    git("merge", "--ff-only", "--quiet", "origin/main")
    revision = git("rev-parse", "HEAD").decode().strip()
    contents = {name: git("show", f"{revision}:{name}") for name in FILES}
    if any(not value for value in contents.values()):
        raise RuntimeError("网页文件为空，停止发布")
    previous = {}
    for name in FILES:
        target = WEBSITE / name
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"网站文件 {name} 已变化，请先核对")
        previous[name] = target.read_bytes()

    now = datetime.now(timezone.utc).isoformat()
    hashes = {name: digest(value) for name, value in contents.items()}
    success_path = STATE / "last-success.json"
    success = json.loads(success_path.read_text()) if success_path.exists() else {}
    changed = previous != contents

    if changed:
        backup = STATE / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        backup.mkdir(parents=True, mode=0o700)
        for name, value in previous.items():
            replace_file(backup / name, value, 0o600)
        replace_file(
            backup / "manifest.json",
            json.dumps({"previous_revision": success.get("revision"), "files": {
                name: digest(value) for name, value in previous.items()
            }}, indent=2).encode() + b"\n", 0o600,
        )
        try:
            for name, value in contents.items():
                replace_file(WEBSITE / name, value)
            check_served_files(contents)
        except Exception:
            for name, value in previous.items():
                replace_file(WEBSITE / name, value)
            print(f"ROLLBACK: 网页文件已恢复；备份 {backup}", flush=True)
            raise
        success["published_at"] = now
        success["backup"] = str(backup)

    if changed or success.get("revision") != revision:
        success.update(revision=revision, checked_at=now, files=hashes)
        replace_file(success_path, json.dumps(success, indent=2).encode() + b"\n", 0o600)
        print(f"{'PUBLISHED' if changed else 'UNCHANGED'} {revision}", flush=True)
        for name, value in hashes.items():
            print(f"{name} SHA256 {value}", flush=True)


if __name__ == "__main__":
    with (STATE / "publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            publish()
        except subprocess.CalledProcessError as error:
            print(error.stderr.decode(errors="replace"), flush=True)
            raise
