"""任务存储与后台流程。

每个任务一个目录 data/jobs/{id}/：
    meta.json          状态与元信息（唯一事实来源）
    images/0.jpg ...   预处理后的送审图
    model.glb          成功后下载的模型
"""

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import aholo, config, prep

LOCK = threading.Lock()
# 平台限制同一 API key 同时只能有一个生成任务，多个任务在此排队
PLATFORM_LOCK = threading.Lock()

STATUS_TEXT = {
    "uploading": "上传图片中",
    "reconstructing": "AI 重建中（通常 3~10 分钟）",
    "succeeded": "完成",
    "failed": "失败",
}


def _job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def _meta_path(job_id: str) -> Path:
    return _job_dir(job_id) / "meta.json"


def read_meta(job_id: str) -> dict | None:
    path = _meta_path(job_id)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_meta(job_id: str, meta: dict) -> None:
    with open(_meta_path(job_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)


def update_meta(job_id: str, **fields) -> None:
    with LOCK:
        meta = read_meta(job_id)
        if meta is None:
            return
        meta.update(fields)
        meta["updatedAt"] = time.time()
        write_meta(job_id, meta)


def create_job(name: str, image_files: list[tuple[str, bytes]]) -> dict:
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    job_dir = _job_dir(job_id)
    (job_dir / "images").mkdir(parents=True, exist_ok=True)

    meta = {
        "id": job_id,
        "name": name or "未命名玩偶",
        "status": "uploading",
        "error": None,
        "taskId": None,
        "createdAt": time.time(),
        "updatedAt": time.time(),
        "imageCount": len(image_files),
        "glbUrl": None,
        "zipUrl": None,
    }
    write_meta(job_id, meta)

    for index, (_, data) in enumerate(image_files):
        (job_dir / "images" / f"{index}.jpg").write_bytes(data)

    thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    thread.start()
    return meta


def _sweep_expired() -> None:
    """清理超过保留期的重建记录。JOB_TTL_DAYS=0（默认）时不清理。"""
    ttl = config.JOB_TTL_DAYS * 86400
    if ttl <= 0 or not config.JOBS_DIR.exists():
        return
    now = time.time()
    for path in config.JOBS_DIR.iterdir():
        if not path.is_dir():
            continue
        meta = read_meta(path.name)
        if meta and now - meta.get("createdAt", now) > ttl:
            shutil.rmtree(path, ignore_errors=True)


def list_jobs() -> list[dict]:
    _sweep_expired()
    jobs = []
    if config.JOBS_DIR.exists():
        for path in config.JOBS_DIR.iterdir():
            if path.is_dir():
                meta = read_meta(path.name)
                if meta:
                    jobs.append(public_meta(meta))
    jobs.sort(key=lambda m: m["createdAt"], reverse=True)
    return jobs


def delete_job(job_id: str) -> bool:
    with LOCK:
        job_dir = _job_dir(job_id)
        if not job_dir.exists():
            return False
        shutil.rmtree(job_dir, ignore_errors=True)
        return True


def public_meta(meta: dict) -> dict:
    out = dict(meta)
    out["statusText"] = STATUS_TEXT.get(meta["status"], meta["status"])
    return out


def _set_stage(job_id: str, status: str, **extra) -> None:
    update_meta(job_id, status=status, **extra)


def _run_job(job_id: str) -> None:
    """后台线程：预处理 -> 上传 -> 创建任务 -> 轮询 -> 下载 GLB。"""
    meta = read_meta(job_id)
    if meta is None:
        return
    try:
        if config.MOCK:
            _run_job_mock(job_id)
            return

        job_dir = _job_dir(job_id)
        image_paths = sorted((job_dir / "images").glob("*.jpg"))
        if not image_paths:
            raise aholo.AholoError("任务目录里没有图片")

        urls = []
        for path in image_paths:
            urls.append(aholo.upload_image(path.read_bytes(), path.name))

        _set_stage(job_id, "reconstructing")
        with PLATFORM_LOCK:
            task_id = aholo.create_img_to_3d_task(urls)
            update_meta(job_id, taskId=task_id)

            detail = aholo.poll_task(
                task_id, on_tick=lambda s: update_meta(job_id, platformStatus=s)
            )
        _collect_outputs(job_id, job_dir, detail)
    except Exception as exc:  # 任何一步失败都落到 failed 状态，前端可见
        update_meta(job_id, status="failed", error=str(exc))


def _collect_outputs(job_id: str, job_dir: Path, detail: dict) -> None:
    import httpx

    outputs = [o.get("content") or "" for o in detail.get("outputs") or []]
    glb_url = next((u for u in outputs if u.lower().endswith(".glb")), None)
    zip_url = next((u for u in outputs if u.lower().endswith(".zip")), None)
    if not glb_url:
        glb_url = next((u for u in outputs if u and u != "NOT_REQUESTED"), None)
    if not glb_url:
        raise aholo.AholoError(f"任务成功但没有可用的模型输出: {outputs}")

    resp = httpx.get(glb_url, timeout=300.0, follow_redirects=True)
    resp.raise_for_status()
    (job_dir / "model.glb").write_bytes(resp.content)
    update_meta(job_id, status="succeeded", glbUrl=glb_url, zipUrl=zip_url)


def _run_job_mock(job_id: str) -> None:
    """演练模式：不调真实接口，走完状态流程并复制一份示例模型。"""
    sample = Path(
        r"C:\Users\nebulaweek\Documents\Codex\2026-08-27\https-skillhub-cn-skills-user-97275c6e\outputs\puppy_model.glb"
    )
    update_meta(job_id, platformStatus=1)
    time.sleep(3)
    _set_stage(job_id, "reconstructing")
    update_meta(job_id, taskId=999000001, platformStatus=1)
    time.sleep(6)
    job_dir = _job_dir(job_id)
    if sample.exists():
        shutil.copyfile(sample, job_dir / "model.glb")
        update_meta(job_id, status="succeeded", glbUrl="mock://puppy_model.glb")
    else:
        update_meta(job_id, status="failed", error="演练模式找不到示例 GLB")
