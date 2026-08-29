"""AHOLO 开放平台客户端：上传图片、创建 Lux3D 图生3D任务、轮询结果。

流程（与 openapi.json 一致）：
1. GET  /asset/v1/token                -> ousToken / globalDomain
2. POST {globalDomain}/ous/api/v2/single/upload   (header: ous-token-v2)
3. GET  {globalDomain}/ous/api/v2/upload/status   直到 d.status == 5，取 d.url
4. POST /lux3d/v1/generate/img-to-3d/task/create  -> d 为任务 id
5. GET  /lux3d/v1/generate/task/get?taskid=      直到 d.status == 3
"""

import hashlib
import time

import httpx

from . import config


class AholoError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=60.0)


def _check_c(resp_json: dict, what: str) -> dict:
    if resp_json.get("c") != "0":
        raise AholoError(f"{what}失败: {resp_json.get('m') or resp_json}")
    return resp_json


def get_upload_token() -> dict:
    with _client() as client:
        resp = client.get(
            f"{config.API_BASE}/asset/v1/token",
            headers={"Authorization": config.API_KEY},
        )
    if resp.status_code == 401:
        raise AholoError("API key 无效（401），请检查 AHOLO_API_KEY")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ousToken"):
        raise AholoError(f"获取上传凭证失败: {data}")
    return data


def upload_image(file_bytes: bytes, filename: str = "photo.jpg") -> str:
    """上传一张图片，返回可直接访问的 URL。每个文件单独取凭证，轮询到 status=5。"""
    token = get_upload_token()
    md5 = hashlib.md5(file_bytes).hexdigest()
    headers = {"ous-token-v2": token["ousToken"]}
    with _client() as client:
        resp = client.post(
            f"{token['globalDomain']}/ous/api/v2/single/upload",
            headers=headers,
            data={"md5": md5},
            files={"file": (filename, file_bytes, "image/jpeg")},
        )
        resp.raise_for_status()
        _check_c(resp.json(), "上传图片")

        deadline = time.time() + 120
        while time.time() < deadline:
            status_resp = client.get(
                f"{token['globalDomain']}/ous/api/v2/upload/status",
                headers=headers,
            )
            status_resp.raise_for_status()
            body = _check_c(status_resp.json(), "查询上传状态")
            detail = body.get("d") or {}
            if detail.get("status") == 5:
                if detail.get("md5") and detail["md5"] != md5:
                    raise AholoError("上传校验失败：MD5 不一致")
                url = detail.get("url")
                if not url:
                    raise AholoError(f"上传完成但未返回 URL: {detail}")
                return url
            time.sleep(1.0)
    raise AholoError("上传状态轮询超时（120 秒）")


def create_img_to_3d_task(image_urls: list[str]) -> int:
    payload = {
        "imgs": image_urls,
        "version": config.LUX3D_VERSION,
        "faceCount": config.FACE_COUNT,
        "outputFormat": ["zip", "glb"],
    }
    with _client() as client:
        resp = client.post(
            f"{config.API_BASE}/lux3d/v1/generate/img-to-3d/task/create",
            headers={"Authorization": config.API_KEY},
            json=payload,
        )
    resp.raise_for_status()
    body = _check_c(resp.json(), "创建重建任务")
    task_id = body.get("d")
    if not task_id:
        raise AholoError(f"任务创建成功但未返回 taskid: {body}")
    return int(task_id)


def get_task(task_id: int) -> dict:
    with _client() as client:
        resp = client.get(
            f"{config.API_BASE}/lux3d/v1/generate/task/get",
            headers={"Authorization": config.API_KEY},
            params={"taskid": task_id},
        )
    resp.raise_for_status()
    body = _check_c(resp.json(), "查询重建任务")
    detail = body.get("d") or {}
    if detail.get("taskId") not in (None, task_id):
        raise AholoError(f"任务查询返回了别的任务: {detail.get('taskId')}")
    return detail


def poll_task(task_id: int, on_tick=None) -> dict:
    """轮询直到任务终态。成功返回任务详情，失败抛 AholoError。"""
    deadline = time.time() + config.TASK_TIMEOUT_SECONDS
    while time.time() < deadline:
        detail = get_task(task_id)
        status = detail.get("status")
        if status == 3:
            return detail
        if status in (4, 6):
            raise AholoError(f"平台任务失败（状态码 {status}）")
        if on_tick:
            on_tick(status)
        time.sleep(config.POLL_INTERVAL_SECONDS)
    raise AholoError("重建轮询超时")
