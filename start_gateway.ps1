# Qwen AI Gateway - PowerShell Launcher
$Host.UI.RawUI.WindowTitle = "Qwen AI Gateway (172.16.8.170:8000)"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  CUTM AI GATEWAY - PRIMARY GPU WORKSTATION CONTROLLER" -ForegroundColor Green
Write-Host "  Local IP: 172.16.8.170  |  Port: 8000" -ForegroundColor Yellow
Write-Host "  Forwarding Gateway: https://qwen.cutm.ac.in -> http://172.16.8.170:8000" -ForegroundColor White
Write-Host "======================================================================" -ForegroundColor Cyan

# 1. Check Ollama
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 -ErrorAction Stop
    Write-Host "[OK] Local Ollama service is running on port 11434." -ForegroundColor Green
} catch {
    Write-Host "[INFO] Starting Ollama background daemon..." -ForegroundColor Yellow
    $env:OLLAMA_HOST = "0.0.0.0:11434"
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

# 2. Check NVIDIA GPU
try {
    $gpu = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null
    if ($gpu) {
        Write-Host "[OK] GPU Detected: $gpu" -ForegroundColor Green
    }
} catch {}

# 3. Environment configuration
$env:PYTHONUNBUFFERED = "1"
$env:HF_HOME = "D:\hf_cache"

Write-Host "`n[STARTING] Launching Backend/qwen_lb.py on http://0.0.0.0:8000...`n" -ForegroundColor Cyan

while ($true) {
    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        & py -3.13 Backend/qwen_lb.py
    } else {
        & python Backend/qwen_lb.py
    }
    Write-Host "`n[NOTICE] Backend exited. Restarting in 3 seconds... (Press Ctrl+C to stop)" -ForegroundColor Red
    Start-Sleep -Seconds 3
}
