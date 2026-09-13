<#
    夸克中转站 / QuarkRelay 打包脚本（PyInstaller 单文件）

    用法：
        powershell -ExecutionPolicy Bypass -File scripts\build.ps1
        powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -SkipTests

    产物：dist\QuarkRelay.exe
    注意：本脚本只做本地打包，不会提交代码、不会发 Release。
#>
[CmdletBinding()]
param(
    [switch]$SkipTests   # 跳过自检（只在已经确认代码没问题时用）
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 让子进程的中文输出不乱码（PowerShell 5.1 默认按 GBK 解码管道）
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Assert-Exit([string]$What) {
    if ($LASTEXITCODE -ne 0) {
        throw "$What 失败（退出码 $LASTEXITCODE）"
    }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "找不到 python，请先安装 Python 3.10+ 并加入 PATH"
}

$Version = (& python -c "import sys; sys.path.insert(0,'.'); from quarkrelay import __version__; print(__version__)").Trim()
Assert-Exit "读取版本号"
Write-Host "夸克中转站 v$Version" -ForegroundColor Green

# 1) 生成内置文档（关于页要显示功能清单和更新公告）
Write-Step "生成内置文档 docs_content.py"
& python scripts\build-docs.py
Assert-Exit "生成内置文档"

# 2) 自检
if (-not $SkipTests) {
    Write-Step "运行自检"
    & python scripts\selftest.py
    Assert-Exit "自检"
}

# 3) 生成 exe 图标
Write-Step "生成程序图标"
& python scripts\make-icon.py
Assert-Exit "生成图标"

# 4) PyInstaller
Write-Step "PyInstaller 打包（单文件，首次会比较慢）"
$Excludes = @(
    'tkinter', 'unittest', 'pydoc', 'doctest', 'pdb', 'lib2to3', 'test',
    'PySide6.Qt3DAnimation', 'PySide6.Qt3DCore', 'PySide6.Qt3DExtras', 'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic', 'PySide6.Qt3DRender', 'PySide6.QtBluetooth', 'PySide6.QtCharts',
    'PySide6.QtDataVisualization', 'PySide6.QtDesigner', 'PySide6.QtHelp',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtNfc',
    'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtRemoteObjects',
    'PySide6.QtScxml', 'PySide6.QtSerialPort', 'PySide6.QtSensors', 'PySide6.QtSpatialAudio',
    'PySide6.QtStateMachine', 'PySide6.QtTest', 'PySide6.QtTextToSpeech',
    'PySide6.QtWebEngineQuick', 'PySide6.QtWebKit', 'PySide6.QtWebKitWidgets'
)
$ArgsList = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'QuarkRelay',
    '--distpath', (Join-Path $Root 'dist'),
    '--workpath', (Join-Path $Root 'build\pyinstaller'),
    '--specpath', (Join-Path $Root 'build'),
    '--icon', (Join-Path $Root 'build\app.ico'),
    '--hidden-import', 'PySide6.QtWebEngineWidgets',
    '--hidden-import', 'PySide6.QtWebEngineCore',
    # 夸克的登录二维码是 <svg>，本地光栅化要用它（代码里是延迟导入，怕收拾不干净）
    '--hidden-import', 'PySide6.QtSvg',
    '--hidden-import', 'PySide6.QtNetwork'
) + ($Excludes | ForEach-Object { '--exclude-module'; $_ }) + @((Join-Path $Root 'main.py'))

& python @ArgsList
Assert-Exit "PyInstaller 打包"

$Exe = Join-Path $Root 'dist\QuarkRelay.exe'
if (-not (Test-Path $Exe)) { throw "没有生成 $Exe" }
$SizeMb = [math]::Round((Get-Item $Exe).Length / 1MB, 1)

Write-Step "打包完成"
Write-Host "产物：dist\QuarkRelay.exe（$SizeMb MB）" -ForegroundColor Green

# 5) 冒烟：离屏装配界面与内置浏览器，确认打包后 WebEngine 还能用
Write-Step "启动冒烟测试（界面 + 内置浏览器）"
$Report = Join-Path $env:TEMP 'quarkrelay-selfcheck.json'
Remove-Item $Report -ErrorAction SilentlyContinue

& $Exe --check | Out-Null
$Code = $LASTEXITCODE

if (-not (Test-Path $Report)) {
    throw "exe 没有产出自检报告（退出码 $Code），可能根本没启动起来"
}

$SelfCheck = Get-Content $Report -Raw -Encoding UTF8 | ConvertFrom-Json
if ($SelfCheck.version -ne $Version) {
    throw "exe 里的版本号是 v$($SelfCheck.version)，期望 v$Version"
}
if (-not $SelfCheck.ok) {
    throw "exe 自检未通过：$($SelfCheck.problems -join '；')"
}
if ($Code -ne 0) {
    Write-Warning "exe 退出码是 $Code（报告显示一切正常，通常是窗口化程序没有控制台导致的，可忽略）"
}

Write-Host ("自检通过：Python {0}，{1} 个页面，内置浏览器 {2}，登录二维码 {3}" -f `
    $SelfCheck.python, $SelfCheck.checks.pages, $SelfCheck.checks.webengine, `
    $SelfCheck.checks.qr_panel) -ForegroundColor Green
Write-Host "版本号 v$Version 校验通过。" -ForegroundColor Green
