# Windows 개발용 도우미(상시 운용 대상은 맥북). 사용법:
#   .\scripts\windows\dev.ps1 setup      # .venv 생성 + 해시 고정 의존성 + aifund setup
#   .\scripts\windows\dev.ps1 test       # 전체 테스트
#   .\scripts\windows\dev.ps1 <aifund 인자...>   # 예: run --mode offline_demo / status / demo --hours 48
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $Root "src"
$env:AIFUND_HOME = $Root
$env:PYTHONIOENCODING = "utf-8"
Set-Location $Root
if ($args.Count -gt 0 -and $args[0] -eq "setup") {
    if (-not (Test-Path $Py)) { py -3.12 -m venv (Join-Path $Root ".venv") }
    & $Py -m pip install --upgrade pip | Out-Null
    & $Py -m pip install --require-hashes -r (Join-Path $Root "requirements-dev.lock")
    & $Py -m aifund setup
    exit $LASTEXITCODE
}
if ($args.Count -gt 0 -and $args[0] -eq "test") { & $Py -m pytest -q; exit $LASTEXITCODE }
& $Py -m aifund @args
exit $LASTEXITCODE
