"""把新增的重建案例自动同步到 GitHub 私有仓库。

Render 免费档的磁盘是临时的：重新部署或重启会回到仓库里的状态。
本模块在后台定时把 data/jobs 下尚未同步的新案例通过 GitHub Git Data API
提交到仓库，使云端生成的模型和记录在重启后依然存在。

配置（环境变量）：
    GITHUB_SYNC_TOKEN  GitHub classic token（repo 权限）。留空 = 关闭同步
    GITHUB_SYNC_REPO   仓库，默认 zhouyufeng123/crochet3d
    SYNC_INTERVAL_MINUTES  同步间隔，默认 30 分钟
"""

import base64
import json
import threading
import time
import traceback
from pathlib import Path

import httpx

from . import config

GH = "https://api.github.com"
SYNC_STATE = Path(config.DATA_DIR) / "sync_state.json"
COMMIT_MESSAGE = "自动同步云端重建案例"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _load_state() -> set:
    if SYNC_STATE.exists():
        try:
            return set(json.loads(SYNC_STATE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_state(state: set) -> None:
    SYNC_STATE.write_text(json.dumps(sorted(state)), encoding="utf-8")


def _job_files(job_dir: Path):
    """收集一个案例目录下所有要上传的文件。"""
    files = []
    for p in sorted(job_dir.rglob("*")):
        if p.is_file():
            files.append((p, p.relative_to(config.JOBS_DIR).as_posix()))
    return files


def _sync_once(token: str, repo: str) -> int:
    """同步一轮，返回上传的文件数。"""
    state = _load_state()
    new_jobs = []
    if config.JOBS_DIR.exists():
        for d in sorted(config.JOBS_DIR.iterdir()):
            if d.is_dir() and d.name not in state and (d / "meta.json").exists():
                new_jobs.append(d)
    if not new_jobs:
        return 0

    with httpx.Client(timeout=120.0) as client:
        h = _headers(token)
        ref = client.get(f"{GH}/repos/{repo}/git/ref/heads/main", headers=h)
        ref.raise_for_status()
        base_sha = ref.json()["object"]["sha"]

        commit_info = client.get(f"{GH}/repos/{repo}/git/commits/{base_sha}", headers=h)
        base_tree = commit_info.json()["tree"]["sha"]

        tree_entries = []
        uploaded_jobs = []
        for job_dir in new_jobs:
            entries = _job_files(job_dir)
            if not entries:
                continue
            ok = True
            for file_path, repo_path in entries:
                content = base64.b64encode(file_path.read_bytes()).decode("ascii")
                blob = client.post(
                    f"{GH}/repos/{repo}/git/blobs",
                    headers=h,
                    json={"content": content, "encoding": "base64"},
                )
                if blob.status_code >= 300:
                    ok = False
                    break
                tree_entries.append(
                    {
                        "path": f"data/jobs/{repo_path}",
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob.json()["sha"],
                    }
                )
            if ok:
                uploaded_jobs.append(job_dir.name)
                state.add(job_dir.name)

        if not tree_entries:
            _save_state(state)
            return 0

        new_tree = client.post(
            f"{GH}/repos/{repo}/git/trees",
            headers=h,
            json={"base_tree": base_tree, "tree": tree_entries},
        )
        new_tree.raise_for_status()

        new_commit = client.post(
            f"{GH}/repos/{repo}/git/commits",
            headers=h,
            json={
                "message": f"自动同步 {len(uploaded_jobs)} 个云端重建案例",
                "tree": new_tree.json()["sha"],
                "parents": [base_sha],
            },
        )
        new_commit.raise_for_status()

        upd = client.patch(
            f"{GH}/repos/{repo}/git/ref/heads/main",
            headers=h,
            json={"sha": new_commit.json()["sha"]},
        )
        upd.raise_for_status()

    _save_state(state)
    return len(tree_entries)


def loop() -> None:
    token = config.GITHUB_SYNC_TOKEN
    repo = config.GITHUB_SYNC_REPO
    interval = config.SYNC_INTERVAL_MINUTES * 60
    while True:
        try:
            files = _sync_once(token, repo)
            if files:
                print(f"[sync] 已同步 {files} 个文件到 GitHub", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(config.SYNC_INTERVAL_MINUTES * 60)
