@echo off
title Qwen AI Gateway - Local RTX 4060 Ti Node (172.16.8.170:8000)
color 0A

echo ======================================================================
echo   QWEN AI GATEWAY - LOCAL GPU WORKSTATION
echo   Host IP: 172.16.8.170  ^|  Port: 8000
echo   Public Domain Proxy: https://qwen.cutm.ac.in
echo ======================================================================
echo.

:: Check Ollama
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] Starting Ollama in background...
    start /B "" ollama serve >nul 2>&1
    timeout /t 3 /nobreak >nul
) else (
    echo [OK] Ollama is already active on port 11434.
)

:: Ensure required environment variables
set PYTHONUNBUFFERED=1
set HF_HOME=D:\hf_cache

echo [INFO] Starting Qwen AI Gateway Backend on 0.0.0.0:8000...
echo.

:loop
py -3.13 Backend\qwen_lb.py
if %ERRORLEVEL% NEQ 0 (
    python Backend\qwen_lb.py
)

echo.
echo [WARNING] Backend process exited. Restarting in 3 seconds... (Press Ctrl+C to stop)
timeout /t 3 /nobreak >nul
goto loop
