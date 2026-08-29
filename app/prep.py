"""照片预处理：自动识别背景色、裁剪主体、铺成正方形白底图。

复刻自之前验证有效的 caterpillar/puppy 预处理流程，让朋友随手拍的照片
不需要任何修图就能直接送进重建。
"""

import io
from collections import Counter

from PIL import Image, ImageChops, ImageOps

OUTPUT_SIZE = 1024
BG_SAMPLE = 12  # 取四角各 12x12 像素估计背景色
DIFF_THRESHOLD = 18


def _background_color(image: Image.Image) -> tuple[int, int, int]:
    width, height = image.size
    corners = []
    for x0, y0 in (
        (0, 0),
        (width - BG_SAMPLE, 0),
        (0, height - BG_SAMPLE),
        (width - BG_SAMPLE, height - BG_SAMPLE),
    ):
        region = image.crop(
            (max(0, x0), max(0, y0), max(0, x0) + BG_SAMPLE, max(0, y0) + BG_SAMPLE)
        )
        pixels = list(region.getdata())
        avg = tuple(sum(c[i] for c in pixels) // len(pixels) for i in range(3))
        corners.append(avg)

    # 四个角里出现最多的颜色区间作为背景（量化到 24 级一档，避免照片噪声干扰）
    def bucket(c):
        return tuple(v // 24 for v in c)

    counts = Counter(bucket(c) for c in corners)
    best = counts.most_common(1)[0][0]
    matches = [c for c in corners if bucket(c) == best]
    return tuple(sum(c[i] for c in matches) // len(matches) for i in range(3))


def prepare_image(raw: bytes) -> bytes:
    """输入原始图片字节，输出 1024x1024 白底主体居中的 JPEG 字节。"""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    background = _background_color(image)

    difference = ImageChops.difference(
        image, Image.new("RGB", image.size, background)
    ).convert("L")
    mask = difference.point(lambda value: 255 if value > DIFF_THRESHOLD else 0)
    bbox = mask.getbbox()

    if bbox:
        left, top, right, bottom = bbox
        area_ratio = (right - left) * (bottom - top) / (image.width * image.height)
        if area_ratio < 0.985:  # 主体基本占满画面时不再裁，避免误伤
            padding = round(min(image.size) * 0.025)
            left = max(0, left - padding)
            top = max(0, top - padding)
            right = min(image.width, right + padding)
            bottom = min(image.height, bottom + padding)
            image = image.crop((left, top, right, bottom))

    side = max(image.size)
    square = Image.new("RGB", (side, side), background)
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    square = square.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    square.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()
