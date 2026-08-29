@echo off
cd /d %~dp0
where python >nul 2>nul || (echo [!] 未找到 Python，请先安装 Python 3.10+ & pause & exit /b 1)
if not exist .venv (
  echo [+] 首次运行，创建虚拟环境并安装依赖...
  python -m venv .venv || (echo [!] 创建虚拟环境失败 & pause & exit /b 1)
  call .venv\Scripts\activate.bat
  pip install -r requirements.txt -q || (echo [!] 依赖安装失败 & pause & exit /b 1)
) else (
  call .venv\Scripts\activate.bat
)
echo.
echo    ============================================
echo      钩织玩偶 3D 重建服务已启动
echo      本机访问:   http://127.0.0.1:8000
echo      局域网访问: http://本机IP:8000
echo      关闭此窗口即停止服务
echo    ============================================
echo.
python run.py
pause
