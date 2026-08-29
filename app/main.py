import os

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, jobs, patterns, prep

app = FastAPI(title="钩织玩偶 3D 重建", docs_url=None, redoc_url=None)

_patterns_cache: list | None = None


def get_patterns() -> list:
    global _patterns_cache
    if _patterns_cache is None:
        # 云端部署时图解文件夹不存在 → 空库，不影响重建功能
        if config.PATTERNS_DIR.exists():
            _patterns_cache = patterns.parse_folder(config.PATTERNS_DIR)
        else:
            _patterns_cache = []
    return _patterns_cache


@app.get("/api/patterns")
def list_patterns():
    out = []
    for p in get_patterns():
        out.append(
            {
                "id": p["id"],
                "name": p["name"],
                "file": p["file"],
                "total": p["total"],
                "roundCount": p["roundCount"],
                "partCount": len(p["parts"]),
                "unparsed": p["unparsed"],
                "yarn": p["yarn"],
                "hook": p["hook"],
            }
        )
    return out


@app.get("/api/patterns/{pid}")
def pattern_detail(pid: str):
    for p in get_patterns():
        if p["id"] == pid:
            return p
    raise HTTPException(404, "图解不存在")


def check_access(code: str = "") -> None:
    """设置了访问口令时，校验请求头 X-Access-Code。"""
    if config.ACCESS_PASSWORD and code != config.ACCESS_PASSWORD:
        raise HTTPException(status_code=401, detail="需要访问口令")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mock": config.MOCK,
        "keyConfigured": bool(config.API_KEY),
        "needAccessCode": bool(config.ACCESS_PASSWORD),
        "version": config.LUX3D_VERSION,
    }


@app.post("/api/jobs")
async def create_job(
    files: list[UploadFile] = File(...),
    name: str = Form(""),
    access_code: str = Form(""),
):
    check_access(access_code)
    if not files:
        raise HTTPException(400, "请至少上传一张图片")
    if len(files) > config.MAX_IMAGES:
        raise HTTPException(400, f"最多支持 {config.MAX_IMAGES} 张图片")

    prepared: list[tuple[str, bytes]] = []
    for file in files:
        if file.content_type not in config.ALLOWED_TYPES:
            raise HTTPException(400, f"不支持的图片格式: {file.filename}（请用 JPG/PNG/WebP）")
        raw = await file.read()
        if len(raw) > 25 * 1024 * 1024:
            raise HTTPException(400, f"图片太大: {file.filename}（上限 25MB）")
        try:
            prepared.append((file.filename or "photo.jpg", prep.prepare_image(raw)))
        except Exception:
            raise HTTPException(400, f"图片无法解析: {file.filename}")

    meta = jobs.create_job(name.strip(), prepared)
    return jobs.public_meta(meta)


@app.get("/api/jobs")
def list_jobs(access_code: str = ""):
    check_access(access_code)
    return jobs.list_jobs()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, access_code: str = ""):
    check_access(access_code)
    meta = jobs.read_meta(job_id)
    if meta is None:
        raise HTTPException(404, "任务不存在")
    return jobs.public_meta(meta)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, access_code: str = ""):
    check_access(access_code)
    if not jobs.delete_job(job_id):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/model.glb")
def job_model(job_id: str, access_code: str = ""):
    check_access(access_code)
    path = jobs._job_dir(job_id) / "model.glb"
    if not path.exists():
        raise HTTPException(404, "模型尚未生成")
    return FileResponse(path, media_type="model/gltf-binary", filename="model.glb")


@app.get("/api/jobs/{job_id}/stitches")
def job_stitches(
    job_id: str,
    axis: str = "auto",
    gaugeW: str = "2.6",
    gaugeH: float = 3.0,
    realSize: float | None = None,
    autoScale: int = 0,
    access_code: str = "",
):
    check_access(access_code)
    meta = jobs.read_meta(job_id)
    if meta is None:
        raise HTTPException(404, "任务不存在")
    if meta["status"] != "succeeded":
        raise HTTPException(400, "任务尚未完成")
    path = jobs._job_dir(job_id) / "model.glb"
    if not path.exists():
        raise HTTPException(404, "模型文件不存在")

    auto = gaugeW.strip().lower() == "auto"
    if not auto:
        try:
            gauge_w = float(gaugeW)
        except ValueError:
            raise HTTPException(400, "gaugeW 需为数字或 auto")
        if not (1.0 <= gauge_w <= 8.0):
            raise HTTPException(400, "密度参数超出范围（1-8）")
    else:
        gauge_w = 2.6  # auto 模式下的占位值，实际密度由检测决定
    if not (1.0 <= gaugeH <= 8.0):
        raise HTTPException(400, "密度参数超出范围（1-8）")
    if realSize is not None and not (2.0 <= realSize <= 500.0):
        raise HTTPException(400, "实际尺寸超出范围（2-500cm）")
    if axis not in ("auto", "x", "y", "z"):
        raise HTTPException(400, "axis 需为 auto/x/y/z")
    try:
        from . import stitches

        return stitches.analyze(
            path,
            axis=axis,
            gauge_w=gauge_w,
            gauge_h=gaugeH,
            real_size_cm=realSize,
            auto=auto,
            auto_scale=bool(autoScale),
        )
    except Exception as exc:
        raise HTTPException(500, f"针数分析失败: {exc}")


@app.get("/api/jobs/{job_id}/images/{index}.jpg")
def job_image(job_id: str, index: int, access_code: str = ""):
    check_access(access_code)
    path = jobs._job_dir(job_id) / "images" / f"{index}.jpg"
    if not path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type="image/jpeg")


static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.exception_handler(404)
def not_found(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": getattr(exc, "detail", "Not Found")}, status_code=404)
    return FileResponse(os.path.join(static_dir, "index.html"))
