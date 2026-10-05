@echo off
rem =====================================================================
rem  i学习 作业评审平台 - 一键启动
rem  说明：
rem    1) 本文件是 GBK(ANSI) 编码 + CRLF 换行，不要用 UTF-8 另存，
rem       也不要在里面加 chcp 65001，否则 cmd 解析会错乱。
rem    2) 变量统一用 !VAR! 延时展开：EDGE_EXE 里含 (x86)，
rem       用 %VAR% 在 if(...) 块里展开会被那个右括号截断。
rem =====================================================================
setlocal EnableExtensions EnableDelayedExpansion

rem ================= 可修改的配置 =================
set "BACKEND_PORT=8010"
set "FRONTEND_PORT=5173"
set "DEBUG_PORT=9222"
set "EDGE_EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "EDGE_PROFILE=%LOCALAPPDATA%\EdgeCrawler"
set "ISTUDY_URL=https://istudy.szpu.edu.cn/portal"
set "PLATFORM_URL=http://localhost:5173"
rem ===============================================

set "ROOT=%~dp0"
if "!ROOT:~-1!"=="\" set "ROOT=!ROOT:~0,-1!"

echo ==================================================
echo    i学习 作业评审平台 - 一键启动
echo ==================================================
echo    项目目录: !ROOT!
echo.

if not exist "!EDGE_EXE!" (
    echo    [警告] 没找到 Edge: !EDGE_EXE!
    echo           请修改本文件开头的 EDGE_EXE 变量。
    echo.
)

rem ---------- 1/4 后端 ----------
call :port_in_use %BACKEND_PORT%
if not errorlevel 1 (
    echo [1/4] 后端已经在 %BACKEND_PORT% 端口运行，跳过
) else (
    echo [1/4] 启动后端（第一次要同步依赖，可能稍慢）...
    start "评审-后端" /D "!ROOT!\backend" cmd /k "uv run uvicorn app.main:app --host 127.0.0.1 --port %BACKEND_PORT% --reload"
)

rem ---------- 2/4 前端 ----------
call :port_in_use %FRONTEND_PORT%
if not errorlevel 1 (
    echo [2/4] 前端已经在 %FRONTEND_PORT% 端口运行，跳过
) else (
    echo [2/4] 启动前端...
    start "评审-前端" /D "!ROOT!\frontend" cmd /k "npm run dev"
)

rem ---------- 3/4 爬虫专用 Edge ----------
call :port_in_use %DEBUG_PORT%
if not errorlevel 1 (
    echo [3/4] i学习 浏览器已经在运行，跳过
) else (
    echo [3/4] 打开 i学习 浏览器（登录态由它提供）...
    start "i学习浏览器" "!EDGE_EXE!" --remote-debugging-port=%DEBUG_PORT% --user-data-dir="!EDGE_PROFILE!" --no-first-run --no-default-browser-check "!ISTUDY_URL!"
)

rem ---------- 4/4 等服务就绪后打开平台页 ----------
echo [4/4] 等待后端与前端就绪...
call :wait_port %BACKEND_PORT% 150 "后端"
call :wait_port %FRONTEND_PORT% 90 "前端"

echo.
echo 打开平台页面: !PLATFORM_URL!
start "评审平台" "!EDGE_EXE!" "!PLATFORM_URL!"

echo.
echo ==================================================
echo   已就绪
echo.
echo     平台页面 : !PLATFORM_URL!
echo     接口文档 : http://127.0.0.1:%BACKEND_PORT%/docs
echo     后端日志 : 窗口「评审-后端」
echo     前端日志 : 窗口「评审-前端」
echo.
echo   提醒: 「一键获取学生名单」需要「i学习浏览器」窗口开着并已登录。
echo   关闭时双击 stop.bat。
echo ==================================================
echo.
pause
exit /b 0

rem ---------------------------------------------------------------
rem 端口是否被监听（被监听 -> errorlevel 0）
:port_in_use
netstat -ano | findstr /c:":%~1 " | findstr /c:"LISTENING" >nul 2>&1
exit /b %errorlevel%

rem 等待端口就绪：参数1=端口 参数2=最多等待秒数 参数3=名称
:wait_port
setlocal EnableDelayedExpansion
set "WP_PORT=%~1"
set "WP_TRIES=%~2"
set "WP_NAME=%~3"
:wait_loop
call :port_in_use %WP_PORT%
if not errorlevel 1 (
    echo           !WP_NAME! 已就绪（端口 !WP_PORT!）
    endlocal & exit /b 0
)
set /a WP_TRIES-=1
if !WP_TRIES! leq 0 (
    echo           [警告] !WP_NAME! 等待超时，请看「评审-!WP_NAME!」窗口里的报错
    endlocal & exit /b 1
)
rem 用 ping 代替 timeout 等待 1 秒：timeout 在输入被重定向时会直接报错退出
ping -n 2 127.0.0.1 >nul
goto wait_loop
