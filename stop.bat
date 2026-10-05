@echo off
rem =====================================================================
rem  静默关闭「i学习 作业评审平台」的后端与前端。
rem  双击即执行，无提示、不需要按键。
rem
rem  说明：直接结束进程（不等进程自己收尾）。这不影响数据安全：
rem        已提交的数据都写在 SQLite 主库里，未提交的事务本来也不会保存。
rem        唯一副作用：若此时有 AI 评审在跑，那条记录会停在「评审中」，
rem        重启后重新点一次评审即可。
rem
rem  本文件是 GBK(ANSI) 编码 + CRLF 换行，编辑时不要改成 UTF-8。
rem =====================================================================
setlocal EnableExtensions

set "BACKEND_PORT=8010"
set "FRONTEND_PORT=5173"

call :kill_port %BACKEND_PORT%
call :kill_port %FRONTEND_PORT%

rem 顺手把两个日志窗口也收掉（start.bat 起的窗口标题固定为下面两个）
taskkill /F /FI "WINDOWTITLE eq 评审-后端" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq 评审-前端" >nul 2>&1

exit /b 0

rem ---------------------------------------------------------------
rem 结束占用指定端口的进程（连同子进程），静默执行
:kill_port
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /c:":%~1 " ^| findstr /c:"LISTENING"') do (
    if not "%%P"=="0" taskkill /PID %%P /F /T >nul 2>&1
)
exit /b 0
