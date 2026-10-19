<#
.SYNOPSIS
    The primary human entry point. Forwards everything to engine/ve.py.

.DESCRIPTION
    This wrapper exists for three lines of environment setup that the Python
    process cannot do for itself, and one that must never be done any other way.

    1. PATH += .venv-asr\Lib\site-packages\torch\lib
       This is THE fix for GPU transcription on Windows. ctranslate2 (inside
       faster-whisper, inside whisperx) links cuDNN by import name; the DLLs
       ship inside the torch wheel and nothing puts that directory on the
       loader path. Without this line CUDA transcription dies partway through
       with an unhelpful DLL error, and the usual "fix" found online is a
       separate multi-gigabyte cuDNN download that is entirely unnecessary.
       It must be set in the PARENT process: the loader reads PATH at DLL bind
       time, and a child that inherits it is what actually matters.

    2. PYTHONUTF8=1
       The Windows console is cp1252. A German transcript containing "Größe"
       raises UnicodeEncodeError on print in a pipeline whose whole point is
       German.

    3. ffmpeg is invoked ONLY as an argv list from Python subprocess. This
       wrapper is PowerShell, never Git Bash, because Git Bash rewrites a
       filter argument containing "C:" into "C\;C:\Program Files\Git\..."
       before ffmpeg ever sees it. Reproduced live, not theorised.

.EXAMPLE
    .\tools\ve.ps1 new ep042 --preset longform --platform youtube
    .\tools\ve.ps1 intake ep042 C:\Videos\cam_a.MP4
    .\tools\ve.ps1 transcribe ep042 --language de
    .\tools\ve.ps1 qc ep042

.NOTES
    Exit code is forwarded verbatim. `ve qc` uses 0 ok / 1 warnings / 2 failures,
    so a hook can branch on it.
#>

# NO param() block on purpose. With one, PowerShell's parameter binder claims
# every token starting with "-" and `ve new x --preset longform` dies with
# "A parameter cannot be found that matches parameter name 'preset'". Without
# one, the automatic $args receives all arguments verbatim, which is exactly
# what a forwarding wrapper needs.

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Entry = Join-Path $RepoRoot 'engine\ve.py'

if (-not (Test-Path -LiteralPath $Entry)) {
    Write-Error "ve: no engine at $Entry"
    exit 1
}

# --- 1. the single line that makes GPU transcription work -------------------
$TorchLib = Join-Path $RepoRoot '.venv-asr\Lib\site-packages\torch\lib'
if (Test-Path -LiteralPath $TorchLib) {
    $current = $env:PATH -split ';'
    if ($current -notcontains $TorchLib) {
        $env:PATH = "$TorchLib;$env:PATH"
    }
}

# --- 2. UTF-8 everywhere ----------------------------------------------------
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# --- 3. locate the system Python --------------------------------------------
# The main pipeline is stdlib-only and runs on the SYSTEM Python (3.14), never
# on .venv-asr. Mixing them is how a third-party import sneaks into the pipeline
# and is never noticed until a machine without the venv runs it.
$Python = $null
foreach ($candidate in @('C:\Python314\python.exe')) {
    if (Test-Path -LiteralPath $candidate) { $Python = $candidate; break }
}
if (-not $Python) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) {
    Write-Error "ve: no Python on PATH and none at C:\Python314\python.exe"
    exit 1
}

$env:VEDIT_ROOT = $RepoRoot

& $Python $Entry @args
exit $LASTEXITCODE
