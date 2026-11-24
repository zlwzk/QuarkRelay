<#
    夸克中转站 / QuarkRelay 打包脚本

    用法：
        powershell -ExecutionPolicy Bypass -File scripts\build.ps1              # 单文件 + 目录版都打
        powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -Mode fast   # 只打目录版（启动快）
        powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -Mode single # 只打单文件版
        powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -SkipTests

    产物：
        dist\QuarkRelay.exe                单文件版（兼容旧用户，启动要解包）
        dist\QuarkRelay\QuarkRelay.exe     目录版（启动快，推荐）
        dist\QuarkRelay-win64.zip          目录版压缩包（Release 资产）
    注意：本脚本只做本地打包，不会提交代码、不会发 Release。
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [ValidateSet('both', 'single', 'fast')]
    [string]$Mode = 'both'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

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

function Invoke-Smoke([string]$Exe, [string]$Version) {
    Write-Step "启动冒烟测试：$(Split-Path $Exe -Leaf)"
    $Report = Join-Path $env:TEMP 'quarkrelay-selfcheck.json'
    Remove-Item $Report -ErrorAction SilentlyContinue
    & $Exe --check | Out-Null
    $Code = $LASTEXITCODE
    if (-not (Test-Path $Report)) {
        throw "$Exe 没有产出自检报告（退出码 $Code），可能根本没启动起来"
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
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "找不到 python，请先安装 Python 3.10+ 并加入 PATH"
}

$Version = (& python -c "import sys; sys.path.insert(0,'.'); from quarkrelay import __version__; print(__version__)").Trim()
Assert-Exit "读取版本号"
Write-Host "夸克中转站 v$Version" -ForegroundColor Green

Write-Step "生成内置文档 docs_content.py"
& python scripts\build-docs.py
Assert-Exit "生成内置文档"

if (-not $SkipTests) {
    Write-Step "运行自检"
    & python scripts\selftest.py
    Assert-Exit "自检"
}

Write-Step "生成程序图标"
& python scripts\make-icon.py
Assert-Exit "生成图标"

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

function Build-App([string]$Layout) {
    $onefile = @()
    if ($Layout -eq 'single') { $onefile = @('--onefile') }
    $ArgsList = @(
        '-m', 'PyInstaller',
        '--noconfirm', '--clean'
    ) + $onefile + @(
        '--windowed',
        '--name', 'QuarkRelay',
        '--distpath', (Join-Path $Root 'dist'),
        '--workpath', (Join-Path $Root "build\pyinstaller-$Layout"),
        '--specpath', (Join-Path $Root 'build'),
        '--icon', (Join-Path $Root 'build\app.ico'),
        '--hidden-import', 'PySide6.QtWebEngineWidgets',
        '--hidden-import', 'PySide6.QtWebEngineCore',
        '--hidden-import', 'PySide6.QtSvg',
        '--hidden-import', 'PySide6.QtNetwork'
    ) + ($Excludes | ForEach-Object { '--exclude-module'; $_ }) + @((Join-Path $Root 'main.py'))
    Write-Step "PyInstaller 打包（$Layout）"
    & python @ArgsList
    Assert-Exit "PyInstaller 打包（$Layout）"
}

if ($Mode -in @('both', 'single')) {
    Build-App 'single'
    $Single = Join-Path $Root 'dist\QuarkRelay.exe'
    if (-not (Test-Path $Single)) { throw "没有生成 $Single" }
    $Mb = [math]::Round((Get-Item $Single).Length / 1MB, 1)
    Write-Step "单文件版完成：dist\QuarkRelay.exe（$Mb MB）"
    Invoke-Smoke $Single $Version
}

if ($Mode -in @('both', 'fast')) {
    Build-App 'fast'
    $Fast = Join-Path $Root 'dist\QuarkRelay\QuarkRelay.exe'
    if (-not (Test-Path $Fast)) { throw "没有生成 $Fast" }
    Write-Step "目录版完成：dist\QuarkRelay\QuarkRelay.exe"
    Invoke-Smoke $Fast $Version

    Write-Step "压缩目录版为 Release 资产"
    $Zip = Join-Path $Root 'dist\QuarkRelay-win64.zip'
    Remove-Item $Zip -ErrorAction SilentlyContinue
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory(
        (Join-Path $Root 'dist\QuarkRelay'), $Zip,
        [IO.Compression.CompressionLevel]::Optimal, $false
    )
    $ZipMb = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
    Write-Host "压缩包：dist\QuarkRelay-win64.zip（$ZipMb MB）" -ForegroundColor Green
}

Write-Host ""
Write-Host "版本号 v$Version 校验通过。" -ForegroundColor Green
