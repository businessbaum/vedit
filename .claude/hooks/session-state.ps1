# SessionStart hook - injects the current pipeline state into the model's context.
#
# WHY a command hook and not a prompt hook: prompt-type hooks are rejected on
# SessionStart in Claude Code 2.1.218 with "no conversation context is available".
# The installed claude-obsidian plugin trips this on every session; do not copy it.
#
# WHY PowerShell and not bash: the only shells guaranteed on this box are
# PowerShell and Git Bash, and Git Bash mangles Windows drive letters in any
# argument it touches. A .ps1 invoked with -File never sees a POSIX path.
#
# Contract: read the hook payload from stdin (ignored - we only need the project
# dir), write ONE line of JSON to stdout, exit 0. Never block a session: any
# failure degrades to an empty additionalContext, never to a non-zero exit.

$ErrorActionPreference = 'Stop'

function AsArray {
    # WHY: on PowerShell 5.1 `@($null)` is a ONE-element array containing $null,
    # so a project.json with no "segments" key would report segments=1. Filtering
    # the nulls is the only spelling that yields a true count for absent keys.
    param($Value)
    return ,@($Value | Where-Object { $null -ne $_ })
}

function Write-HookJson {
    param([string]$Context)
    $payload = @{
        hookSpecificOutput = @{
            hookEventName    = 'SessionStart'
            additionalContext = $Context
        }
    }
    # ConvertTo-Json escapes umlauts to \uXXXX, which keeps the line ASCII-safe
    # regardless of the console code page. -Compress keeps it to one line.
    $json = $payload | ConvertTo-Json -Depth 5 -Compress
    # [Console]::Out avoids the BOM that Out-File/> would prepend on PS 5.1.
    [Console]::Out.Write($json)
    [Console]::Out.Flush()
}

try {
    # Drain stdin so the parent never blocks on a full pipe buffer. The payload
    # ({session_id, transcript_path, cwd, hook_event_name, source}) is unused.
    $null = [Console]::In.ReadToEnd()

    $root = $env:CLAUDE_PROJECT_DIR
    if ([string]::IsNullOrWhiteSpace($root)) { $root = (Get-Location).Path }

    $projectsDir = Join-Path $root 'projects'
    if (-not (Test-Path -LiteralPath $projectsDir)) {
        Write-HookJson 'PIPELINE: no projects/ directory yet. Start with the intake skill.'
        exit 0
    }

    # Most-recently-touched project.json wins. WHY: the user works one episode at
    # a time and expects "continue where I left off" without naming a slug.
    $candidates = @(Get-ChildItem -LiteralPath $projectsDir -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'project.json' } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Get-Item |
        Sort-Object LastWriteTime -Descending)

    if ($candidates.Count -eq 0) {
        Write-HookJson 'PIPELINE: projects/ is empty. Start with the intake skill.'
        exit 0
    }

    $file = $candidates[0]
    $slug = Split-Path (Split-Path $file.FullName -Parent) -Leaf

    $p = $null
    try {
        $p = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-HookJson ("PIPELINE: projects/{0}/project.json does not parse as JSON. Read and repair it before running any stage." -f $slug)
        exit 0
    }

    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add(("project={0}" -f $slug))
    $parts.Add(("stage={0}" -f $(if ($p.stage) { $p.stage } else { 'unknown' })))
    $parts.Add(("schema={0}" -f $(if ($p.schema_version) { $p.schema_version } else { '?' })))

    if ($p.timebase) {
        $den = if ($p.timebase.fps_den) { $p.timebase.fps_den } else { 1 }
        $parts.Add(("fps={0}/{1}" -f $p.timebase.fps_num, $den))
    }
    if ($p.output -and $p.output.platform) { $parts.Add(("platform={0}" -f $p.output.platform)) }
    if ($p.format_preset) { $parts.Add(("preset={0}" -f $p.format_preset)) }

    $sources = AsArray $p.sources
    $parts.Add(("sources={0}" -f $sources.Count))

    $segs = AsArray $p.segments
    $enabled = AsArray ($segs | Where-Object { $_.enabled })
    $locked  = AsArray ($segs | Where-Object { $_.locked })
    $parts.Add(("segments={0} enabled={1} locked={2}" -f $segs.Count, $enabled.Count, $locked.Count))

    if ($p.story -and $p.story.blocks) {
        $blocks = AsArray $p.story.blocks
        $keep   = AsArray ($blocks | Where-Object { $_.decision -eq 'keep' })
        $maybe  = AsArray ($blocks | Where-Object { $_.decision -eq 'maybe' })
        $parts.Add(("story_blocks={0} keep={1} maybe={2}" -f $blocks.Count, $keep.Count, $maybe.Count))
    } else {
        $parts.Add('story=none')
    }

    if ($p.transcript -and $p.transcript.word_count) {
        $parts.Add(("words={0}" -f $p.transcript.word_count))
    }
    if ($p._computed -and $p._computed.total_frames) {
        $parts.Add(("total_frames={0}" -f $p._computed.total_frames))
    }
    if ($p.captions) {
        $parts.Add(("captions_enabled={0}" -f [bool]$p.captions.enabled))
    }

    # WHY surfaced every session: every cut threshold in presets/ is research-derived,
    # not perceptually tested. Until someone runs the calibration fixture, the model
    # must not present cut decisions as tuned.
    if ($null -ne $p.calibrated -and -not $p.calibrated) {
        $parts.Add('calibrated=false (thresholds are UNCALIBRATED defaults - say so before defending a cut)')
    }

    $line = 'PIPELINE_STATE: ' + ($parts -join ' | ')

    if ($candidates.Count -gt 1) {
        $others = @($candidates[1..($candidates.Count - 1)] |
            ForEach-Object { Split-Path (Split-Path $_.FullName -Parent) -Leaf })
        $line += "`nOTHER_PROJECTS: " + ($others -join ', ') + ' (state above is the most recently modified one; confirm the slug before editing)'
    }

    Write-HookJson $line
    exit 0
}
catch {
    # A broken hook must never cost the user a session.
    Write-HookJson ("PIPELINE: state hook failed ({0}). Read projects/*/project.json directly." -f $_.Exception.Message)
    exit 0
}
