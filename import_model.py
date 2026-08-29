"""把已有的 GLB 模型导入记录库（data/jobs），让网页端能看到之前重建的内容。

用法示例：
    python import_model.py --name "毛毛虫" --glb "旧项目/caterpillar_model.glb" --img "旧项目/caterpillar-cropped.jpg"
    python import_model.py --name "小狗" --glb xxx.glb --img a.jpg --img b.jpg --time "2026-08-27 23:45"

--time 省略时用 GLB 文件的修改时间。导入的记录状态直接是"完成"。
"""

import argparse
import json
import shutil
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "data" / "jobs"


def main():
    parser = argparse.ArgumentParser(description="导入已有 GLB 模型到记录库")
    parser.add_argument("--name", required=True, help="记录名称")
    parser.add_argument("--glb", required=True, help="GLB 文件路径")
    parser.add_argument("--img", action="append", default=[], help="输入照片路径，可多次指定")
    parser.add_argument("--time", dest="created", help="创建时间，如 2026-08-27 23:45")
    args = parser.parse_args()

    glb_path = Path(args.glb)
    if not glb_path.exists():
        raise SystemExit(f"GLB 不存在: {glb_path}")

    created = time.time()
    if args.created:
        created = time.mktime(time.strptime(args.created, "%Y-%m-%d %H:%M"))

    job_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(created)) + "-" + uuid.uuid4().hex[:6]
    job_dir = JOBS_DIR / job_id
    (job_dir / "images").mkdir(parents=True, exist_ok=True)

    for index, img_path in enumerate(args.img):
        img_path = Path(img_path)
        if not img_path.exists():
            print(f"[跳过] 图片不存在: {img_path}")
            continue
        # 统一转成适合卡片展示的 JPG
        image = ImageOps.exif_transpose(Image.open(img_path)).convert("RGB")
        image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        image.save(job_dir / "images" / f"{index}.jpg", "JPEG", quality=88)

    shutil.copyfile(glb_path, job_dir / "model.glb")

    meta = {
        "id": job_id,
        "name": args.name,
        "status": "succeeded",
        "error": None,
        "taskId": None,
        "source": "imported",
        "createdAt": created,
        "updatedAt": created,
        "imageCount": len(list((job_dir / "images").glob("*.jpg"))),
        "glbUrl": "imported",
        "zipUrl": None,
    }
    with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    print(f"已导入: {args.name} -> {job_dir}（模型 {glb_path.stat().st_size // 1024 // 1024}MB，{meta['imageCount']} 张图）")


if __name__ == "__main__":
    main()
