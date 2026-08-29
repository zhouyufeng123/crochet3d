"""启动入口：读取 .env 里的 HOST/PORT 后启动服务。"""

import uvicorn

from app import config

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)
