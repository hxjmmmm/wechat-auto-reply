@echo off
chcp 936 >nul
rem %~dp0 = 本 .cmd 所在目录，别写死绝对路径 —— 换台机器/换目录就跑不起来
cd /d "%~dp0"

rem 优先用打包好的 exe（不依赖 Python 环境）
if exist "dist\微信自动回复助手.exe" (
  start "" "dist\微信自动回复助手.exe" --daemon-run
  exit /b 0
)

rem 没有 exe 就退回源码方式：优先用项目自带的 .venv
rem （依赖都装在 .venv 里，不动系统 Python / conda）
rem 代码在 src/ 下，统一从 src\launcher.py 进（和 exe 同一条入口）
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "src\launcher.py" --daemon-run
  exit /b 0
)

rem 连 .venv 都没有才退回 PATH 里的 pythonw
start "" pythonw.exe "src\launcher.py" --daemon-run
