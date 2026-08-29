import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# AHOLO 开放平台 API key：优先 AHOLO_API_KEY，兼容之前用的 LUX3D_API_KEY
API_KEY = os.environ.get("AHOLO_API_KEY") or os.environ.get("LUX3D_API_KEY") or ""
API_BASE = os.environ.get("AHOLO_API_BASE", "https://api.aholo3d.cn")

# 简单访问口令：留空则不启用。发布给朋友前建议设置一个。
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")

# 演练模式：AHOLO_MOCK=1 时不调用真实接口、不消耗额度，用示例模型走完整个流程
MOCK = os.environ.get("AHOLO_MOCK", "") == "1"

LUX3D_VERSION = os.environ.get("LUX3D_VERSION", "G1")  # G1 | G1-Turbo
FACE_COUNT = int(os.environ.get("LUX3D_FACE_COUNT", "200000"))

HOST = os.environ.get("HOST", "0.0.0.0")
# 云平台（如 Render）会注入 PORT 环境变量，优先使用
PORT = int(os.environ.get("PORT", "8000"))

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"

# 图解库文件夹（docx 图解）
PATTERNS_DIR = Path(os.environ.get("PATTERNS_DIR", r"C:\Users\nebulaweek\Desktop\玩偶"))

MAX_IMAGES = 8
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
TASK_TIMEOUT_SECONDS = 40 * 60
POLL_INTERVAL_SECONDS = 10
