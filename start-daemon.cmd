@echo off
chcp 936 >nul
rem %~dp0 = 本 .cmd 所在目录，别写死绝对路径 —— 换台机器/换目录就跑不起来
cd /d "%~dp0"

rem 优先用打包好的 exe（不依赖 Python 环境）
if exist "dist\微信自动回复助手.exe" (
  start "" "dist\微信自动回复助手.exe" --daemon-run
  exit /b 0
)

rem 没有 exe 就退回源码方式：用 PATH 里的 pythonw；
rem 想固定用某个解释器就改成你的完整路径，例如
rem   start "" "C:\Users\<你的用户名>\miniconda3\pythonw.exe" "daemon.py"
start "" pythonw.exe "daemon.py"
