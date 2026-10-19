<#
.SYNOPSIS
    One-shot environment setup for the Editing pipeline. Idempotent.

.DESCRIPTION
    Two Pythons, on purpose:

      system Python 3.14   the whole pipeline. Stdlib only, forever. The moment
                           someone reaches for numpy, cp314 wheel availability
                           becomes a permanent tax.
      .venv-asr (3.12)     WhisperX and torch, reached ONLY via subprocess.
                           WhisperX 3.8.6 declares requires-python <3.14 and
                           hard-pins torch~=2.8.0, which has no cp314 wheels.
                           There is no version of this that runs on 3.14.

    This script prints its entire plan before doing anything, because step 4
    downloads several gigabytes.

    Re-running is safe. Every step checks whether it is already done and says
    so instead of redoing it.

.PARAMETER DryRun
    Print the plan and every command, execute nothing. Same effect as -WhatIf.

.PARAMETER Force
    Reinstall the ASR packages even when whisperx already imports.

.PARAMETER Recreate
    Delete and rebuild .venv-asr from scratch.

.PARAMETER PythonVersion
    CPython version for .venv-asr. Default 3.12 (already on disk locally;
    3.13 also resolves, 3.14 does not).

.EXAMPLE
    .\tools\setup.ps1 -DryRun
    .\tools\setup.ps1

.NOTES
    Exit codes: 0 ready (tier 1 or 2), 1 setup failed, 2 degraded to tier 3.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$DryRun,
    [switch]$Force,
    [switch]$Recreate,
    [string]$PythonVersion = '3.12'
)

$ErrorActionPreference = 'Stop'

# -WhatIf and -DryRun are the same request. Fold -WhatIf into $DryRun and then
# CLEAR $WhatIfPreference: leaving it set makes every cmdlet this script calls
# (and every cmdlet those modules call at import time) print its own WhatIf
# line, burying the plan under a wall of "Set-Alias" noise.
if ($WhatIfPreference) { $DryRun = $true }
$WhatIfPreference = $false

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot '.venv-asr'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$TorchLib = Join-Path $VenvDir 'Lib\site-packages\torch\lib'
$LockFile = Join-Path $RepoRoot 'asr-requirements.lock'
$WhisperXPin = 'whisperx==3.8.6'
$TorchBackend = 'cu128'

$script:Failed = $false

function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('-' * [Math]::Min(78, $Text.Length + 12)) -ForegroundColor DarkGray
}

function Write-Plan([string]$Text) {
    Write-Host "  PLAN  $Text" -ForegroundColor Yellow
}

function Write-Ok([string]$Text) { Write-Host "  ok    $Text" -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host "        $Text" -ForegroundColor Gray }
function Write-Bad([string]$Text) {
    Write-Host "  FAIL  $Text" -ForegroundColor Red
    $script:Failed = $true
}
function Write-Warn2([string]$Text) { Write-Host "  warn  $Text" -ForegroundColor Yellow }

function Get-ToolVersion([string]$Exe, [string[]]$VersionArgs) {
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    try {
        $out = & $cmd.Source @VersionArgs
        return [pscustomobject]@{ Path = $cmd.Source; Version = (($out | Select-Object -First 1) -as [string]).Trim() }
    } catch {
        return [pscustomobject]@{ Path = $cmd.Source; Version = 'unknown' }
    }
}

# Runs a command unless this is a dry run. The plan line is printed either way,
# so -DryRun output and a real run read identically.
function Invoke-Step {
    param(
        [string]$What,
        [string]$Exe,
        [string[]]$CmdArgs,
        [switch]$AllowFailure
    )
    Write-Plan $What
    Write-Info ("$Exe " + ($CmdArgs -join ' '))
    if ($DryRun -or -not $PSCmdlet.ShouldProcess($What, 'run')) {
        Write-Info '(dry run: not executed)'
        return $true
    }
    & $Exe @CmdArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        if ($AllowFailure) {
            Write-Warn2 "exited $code"
            return $false
        }
        Write-Bad "exited $code"
        return $false
    }
    Write-Ok $What
    return $true
}

# =============================================================================
Write-Host ''
Write-Host '  Editing pipeline setup' -ForegroundColor White
Write-Host "  repo: $RepoRoot"
if ($DryRun) { Write-Host '  MODE: DRY RUN -- nothing will be created, downloaded or modified' -ForegroundColor Yellow }
Write-Host ''
Write-Host '  What this will do:' -ForegroundColor White
Write-Host '    1. verify ffmpeg, ffprobe, node, npm, uv and the system Python'
Write-Host '    2. check free space on C:'
Write-Host "    3. create $VenvDir with uv --python $PythonVersion   (~30 MB)"
Write-Host "    4. install $WhisperXPin --torch-backend $TorchBackend   (SEVERAL GIGABYTES, one download)"
Write-Host "    5. write $LockFile so the resolution cannot drift"
Write-Host '    6. run the ctranslate2.get_cuda_device_count() preflight and report tier 1/2/3'
Write-Host ''

# --- 1. tools ----------------------------------------------------------------
Write-Head '1. tools'

$ffmpeg = Get-ToolVersion 'ffmpeg' @('-version')
if ($ffmpeg) { Write-Ok "ffmpeg   $($ffmpeg.Version)" } else { Write-Bad 'ffmpeg not on PATH -- the entire render lane needs it' }

$ffprobe = Get-ToolVersion 'ffprobe' @('-version')
if ($ffprobe) { Write-Ok "ffprobe  $($ffprobe.Version)" } else { Write-Bad 'ffprobe not on PATH -- probing and every QC check needs it' }

$node = Get-ToolVersion 'node' @('-v')
if ($node) { Write-Ok "node     $($node.Version)" } else { Write-Warn2 'node not on PATH -- only the graphics tooling wants it' }

$npm = Get-ToolVersion 'npm' @('-v')
if ($npm) { Write-Ok "npm      $($npm.Version)" } else { Write-Warn2 'npm not on PATH' }

$uv = Get-ToolVersion 'uv' @('--version')
if ($uv) { Write-Ok "uv       $($uv.Version)" } else { Write-Bad 'uv not on PATH -- it is the only supported way to build .venv-asr' }

$sysPython = $null
if (Test-Path -LiteralPath 'C:\Python314\python.exe') {
    $sysPython = 'C:\Python314\python.exe'
} else {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $sysPython = $cmd.Source }
}
if ($sysPython) {
    $pv = & $sysPython -c "import sys;print('.'.join(map(str,sys.version_info[:3])))"
    Write-Ok "python   $pv  ($sysPython)"
    if ([version]$pv -lt [version]'3.11') { Write-Bad "the pipeline needs Python 3.11 or newer" }
} else {
    Write-Bad 'no system Python found'
}

# The main pipeline is stdlib-only. Prove it here rather than discovering it on
# a machine where a stray third-party import is missing.
if ($sysPython -and -not $DryRun) {
    $engine = Join-Path $RepoRoot 'engine'
    if (Test-Path -LiteralPath (Join-Path $engine 've.py')) {
        & $sysPython (Join-Path $engine 've.py') --version | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok 'engine imports on the system Python with no third-party packages' }
        else { Write-Bad 'engine/ve.py --version failed on the system Python' }
    }
}

# libass opens EVERY file in the fonts dir and errors on non-fonts, so a stray
# README in there breaks caption rendering. Cheap to check, annoying to debug.
$fontsDir = Join-Path $RepoRoot 'assets\fonts'
if (Test-Path -LiteralPath $fontsDir) {
    $strays = Get-ChildItem -LiteralPath $fontsDir -File | Where-Object { $_.Extension -notin '.ttf', '.otf', '.ttc' }
    if ($strays) {
        Write-Warn2 ("assets\fonts contains non-font files libass will try to open: " + (($strays | Select-Object -ExpandProperty Name) -join ', '))
    } else {
        Write-Ok ("assets\fonts: " + (Get-ChildItem -LiteralPath $fontsDir -File).Count + ' font file(s), nothing else')
    }
}

if ($script:Failed) {
    Write-Host ''
    Write-Host '  setup cannot continue until the FAIL lines above are fixed.' -ForegroundColor Red
    exit 1
}

# --- 2. disk -----------------------------------------------------------------
Write-Head '2. disk'
$drive = Get-PSDrive -Name ((Split-Path -Qualifier $RepoRoot).TrimEnd(':')) -ErrorAction SilentlyContinue
if ($drive) {
    $freeGb = [Math]::Round($drive.Free / 1GB, 1)
    Write-Ok "$($drive.Name): $freeGb GB free"
    if ($freeGb -lt 20) { Write-Bad "under 20 GB free; the ASR install alone is several GB" }
} else {
    Write-Warn2 'could not read free space'
}
if ($script:Failed) { exit 1 }

# --- 3. venv -----------------------------------------------------------------
Write-Head '3. .venv-asr'

if ($Recreate -and (Test-Path -LiteralPath $VenvDir)) {
    Write-Plan "delete and rebuild $VenvDir"
    if (-not $DryRun) { Remove-Item -LiteralPath $VenvDir -Recurse -Force }
}

if (Test-Path -LiteralPath $VenvPython) {
    $venvVer = & $VenvPython -c "import sys;print('.'.join(map(str,sys.version_info[:3])))"
    Write-Ok "already exists: Python $venvVer at $VenvPython"
} else {
    $created = Invoke-Step -What "create .venv-asr on CPython $PythonVersion" -Exe 'uv' -CmdArgs @('venv', '--python', $PythonVersion, $VenvDir)
    if (-not $created -and -not $DryRun) { exit 1 }
}

# --- 4. packages -------------------------------------------------------------
Write-Head '4. whisperx'

$needInstall = $true
if ((Test-Path -LiteralPath $VenvPython) -and -not $Force) {
    & $VenvPython -c "import whisperx" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $wxv = & $VenvPython -c "import importlib.metadata as m;print(m.version('whisperx'))"
        Write-Ok "whisperx $wxv already installed (pass -Force to reinstall)"
        $needInstall = $false
    }
}

if ($needInstall) {
    Write-Host ''
    Write-Host '  This step downloads SEVERAL GIGABYTES (torch 2.8.0+cu128 and friends).' -ForegroundColor Yellow
    Write-Host '  It runs once. Nothing else in this script is large.' -ForegroundColor Yellow
    Write-Host ''
    $installed = Invoke-Step -What "install $WhisperXPin with the $TorchBackend torch backend" `
        -Exe 'uv' -CmdArgs @('pip', 'install', '--python', $VenvPython, $WhisperXPin, '--torch-backend', $TorchBackend)
    if (-not $installed -and -not $DryRun) { exit 1 }
}

# --- 5. lock -----------------------------------------------------------------
Write-Head '5. asr-requirements.lock'
Write-Plan "freeze the resolved package set to $LockFile"
if ($DryRun) {
    Write-Info 'uv pip freeze --python <venv> > asr-requirements.lock  (dry run: not executed)'
} else {
    $frozen = & uv pip freeze --python $VenvPython
    if ($LASTEXITCODE -eq 0 -and $frozen) {
        $header = @(
            "# asr-requirements.lock -- the exact resolution of $WhisperXPin --torch-backend $TorchBackend",
            "# Regenerate with tools\setup.ps1. Reinstall with:",
            "#   uv pip install --python .venv-asr\Scripts\python.exe -r asr-requirements.lock",
            "# WhisperX pins torch~=2.8.0 and declares requires-python <3.14, which is why this",
            "# lives in a 3.12 sidecar venv and not in the main pipeline.",
            "# frozen: " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        )
        $lines = $header + $frozen
        [IO.File]::WriteAllLines($LockFile, $lines, (New-Object Text.UTF8Encoding $false))
        Write-Ok "$($frozen.Count) packages -> $LockFile"
    } else {
        Write-Warn2 'uv pip freeze produced nothing; lock not written'
    }
}

# --- 6. preflight ------------------------------------------------------------
Write-Head '6. CUDA preflight'
Write-Host '  Two seconds here distinguishes "CUDA is fine" from "the DLL path is wrong" from' -ForegroundColor Gray
Write-Host '  "driver problem" -- three failures that otherwise all present as one opaque crash' -ForegroundColor Gray
Write-Host '  forty minutes into a transcription.' -ForegroundColor Gray
Write-Host ''

$probeSrc = @'
import json, os, pathlib, sys

result = {"import_ok": False, "devices": 0, "compute_types": [], "error": None,
          "torch_lib": None, "ct2_version": None}

# ctranslate2 links cuDNN by import name. The DLLs ship INSIDE the torch wheel
# and nothing puts that directory on the loader path -- this is the entire
# Windows cuDNN failure, and this is the entire fix. No separate cuDNN download.
lib = pathlib.Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
result["torch_lib"] = str(lib)
if lib.is_dir():
    os.add_dll_directory(str(lib))
    os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")

try:
    import ctranslate2 as ct2
    result["import_ok"] = True
    result["ct2_version"] = ct2.__version__
    result["devices"] = ct2.get_cuda_device_count()
    if result["devices"] > 0:
        result["compute_types"] = sorted(ct2.get_supported_compute_types("cuda"))
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"

print("PREFLIGHT_JSON " + json.dumps(result))
'@

$probeFile = Join-Path $env:TEMP 've_preflight.py'
$tier = 3
$probe = $null

if ($DryRun) {
    Write-Plan 'run the ctranslate2 preflight in .venv-asr'
    Write-Info "$VenvPython $probeFile   (dry run: not executed)"
    Write-Host ''
    Write-Host '  dry run complete. Nothing was created, downloaded or modified.' -ForegroundColor Yellow
    exit 0
}

[IO.File]::WriteAllText($probeFile, $probeSrc, (New-Object Text.UTF8Encoding $false))
Write-Plan 'run the ctranslate2 preflight in .venv-asr'
$raw = & $VenvPython $probeFile
Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue

$line = $raw | Where-Object { $_ -like 'PREFLIGHT_JSON *' } | Select-Object -First 1
if ($line) {
    $probe = $line.Substring('PREFLIGHT_JSON '.Length) | ConvertFrom-Json
}

if ($probe -and $probe.import_ok -and $probe.devices -ge 1) {
    $tier = 1
} elseif ($probe -and $probe.import_ok) {
    $tier = 2
} else {
    $tier = 3
}

Write-Host ''
switch ($tier) {
    1 {
        Write-Host '  TIER 1 -- GPU forced alignment' -ForegroundColor Green
        Write-Host "    ctranslate2 $($probe.ct2_version), $($probe.devices) CUDA device(s)"
        Write-Host "    compute types: $($probe.compute_types -join ', ')"
        Write-Host '    whisperx on CUDA, true wav2vec2 forced alignment, real word boundaries.'
        Write-Host '    project.json records timestamp_source: wav2vec2-forced-align'
    }
    2 {
        Write-Host '  TIER 2 -- CPU forced alignment' -ForegroundColor Yellow
        Write-Host "    ctranslate2 $($probe.ct2_version) imports, but reports 0 CUDA devices."
        Write-Host '    The same venv still works with --device cpu --compute_type int8:'
        Write-Host '    zero CUDA DLLs needed, real forced alignment, just slow.'
        Write-Host '    project.json still records timestamp_source: wav2vec2-forced-align'
        Write-Host ''
        Write-Host "    To reach tier 1, check that $TorchLib exists and holds cudnn_*64_9.dll."
    }
    3 {
        Write-Host '  TIER 3 -- degraded, token-level timestamps only' -ForegroundColor Red
        if ($probe) { Write-Host "    preflight error: $($probe.error)" }
        else { Write-Host "    preflight produced no readable output:"; $raw | ForEach-Object { Write-Host "      $_" } }
        Write-Host '    Fall back to the ffmpeg whisper filter, which needs no install at all.'
        Write-Host '    Mark those transcripts timestamp_source: ffmpeg-whisper-token so the cut'
        Write-Host '    stage knows the boundaries are coarser and widens its padding.'
    }
}

Write-Host ''
Write-Head 'next'
Write-Host '  .\tools\ve.ps1 new ep042 --preset longform --platform youtube'
Write-Host '  .\tools\ve.ps1 intake ep042 C:\Videos\cam_a.MP4'
Write-Host '  .\tools\ve.ps1 transcribe ep042 --language de'
Write-Host '  .\tools\ve.ps1 status ep042'
Write-Host ''

if ($tier -eq 3) { exit 2 }
exit 0
