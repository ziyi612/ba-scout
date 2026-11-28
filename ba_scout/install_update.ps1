param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][ValidatePattern('^ba-scout-update-[a-z0-9_]+$')][string]$StageName,
    [Parameter(Mandatory = $true)][ValidateRange(0, 2147483647)][int]$ParentPid,
    [switch]$NoRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$mutex = $null
$stage = $null
$backup = $null
$jobs = @()
$success = $false
$restored = $true
$restartAllowed = $false
$message = ''

function Get-SafeItem([string]$Path, [bool]$IsDirectory, [bool]$Optional = $false) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($Optional) { return $null }
        throw "Missing update file or directory: $Path"
    }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $item.PSIsContainer -ne $IsDirectory) {
        throw "Invalid update file or directory: $Path"
    }
    return $item
}

function Move-UpdateItem([string]$Source, [string]$Target, [bool]$IsDirectory) {
    if ($IsDirectory) {
        [IO.Directory]::Move($Source, $Target)
    } else {
        [IO.File]::Move($Source, $Target)
    }
}

try {
    $Root = [IO.Path]::GetFullPath($Root)
    $tempPath = Join-Path $Root 'tmp'
    $stagePath = Join-Path $tempPath $StageName
    $null = Get-SafeItem $Root $true
    $null = Get-SafeItem $tempPath $true
    $null = Get-SafeItem $stagePath $true
    if ($PSScriptRoot -ne $stagePath) { throw 'The installer is outside the update staging directory.' }
    $stage = $stagePath
    $dataPath = Join-Path $Root 'data'
    $payload = Join-Path $stage 'payload'
    $payloadData = Join-Path $payload 'data'

    if ($ParentPid -gt 0) {
        $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
        if ($null -ne $parent) {
            try {
                if (-not $parent.WaitForExit(120000)) { throw 'Timed out waiting for BA Scout to shut down.' }
            } finally {
                $parent.Dispose()
            }
        }
    }
    # The same named mutex as the packaged app prevents launching into a
    # partially swapped runtime. If another app instance won, leave it alone.
    $created = $false
    $mutex = [Threading.Mutex]::new($false, 'Local\BrokenArrowScout_Portable_06', [ref]$created)
    if (-not $created) { throw 'BA Scout is already running. Close it before retrying the update.' }
    $restartAllowed = $true

    $null = Get-SafeItem $dataPath $true
    $null = Get-SafeItem $payload $true
    $null = Get-SafeItem $payloadData $true
    $backup = Join-Path $stage 'backup'
    $jobs = @(
        @{ Name = '_internal'; SourceRoot = $payload; TargetRoot = $Root; Directory = $true; Optional = $false; MovedOld = $false; MovedNew = $false },
        @{ Name = 'prep-layout-default.json'; SourceRoot = $payloadData; TargetRoot = $dataPath; Directory = $false; Optional = $true; MovedOld = $false; MovedNew = $false },
        @{ Name = 'BA-Scout.exe'; SourceRoot = $payload; TargetRoot = $Root; Directory = $false; Optional = $false; MovedOld = $false; MovedNew = $false }
    )
    foreach ($job in $jobs) {
        $job.Source = Join-Path $job.SourceRoot $job.Name
        $job.Target = Join-Path $job.TargetRoot $job.Name
        $job.Backup = Join-Path $backup $job.Name
        $null = Get-SafeItem $job.Source $job.Directory
        $job.HadOld = $null -ne (Get-SafeItem $job.Target $job.Directory $job.Optional)
    }
    if (Test-Path -LiteralPath $backup) { throw 'The update backup directory already exists.' }
    $null = [IO.Directory]::CreateDirectory($backup)
    $null = Get-SafeItem $backup $true
    foreach ($job in $jobs) {
        if ($job.HadOld) {
            Move-UpdateItem $job.Target $job.Backup $job.Directory
            $job.MovedOld = $true
        }
        Move-UpdateItem $job.Source $job.Target $job.Directory
        $job.MovedNew = $true
    }
    $success = $true
    $message = "BA Scout update installed. Previous runtime: $backup"
} catch {
    $message = "BA Scout update failed: $($_.Exception.Message)"
    for ($index = $jobs.Count - 1; $index -ge 0; $index--) {
        $job = $jobs[$index]
        try {
            if ($job.MovedNew) { Move-UpdateItem $job.Target $job.Source $job.Directory }
            if ($job.MovedOld) { Move-UpdateItem $job.Backup $job.Target $job.Directory }
        } catch {
            $restored = $false
            $message += " Rollback failed for $($job.Name): $($_.Exception.Message)"
        }
    }
    if (-not $restored -and $null -ne $backup) { $message += " Previous files: $backup" }
} finally {
    if ($null -ne $mutex) { $mutex.Dispose() }
}

if ($null -ne $stage) {
    try {
        $result = [IO.FileStream]::new((Join-Path $stage 'install-result.txt'), [IO.FileMode]::CreateNew)
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($message)
            $result.Write($bytes, 0, $bytes.Length)
        } finally {
            $result.Dispose()
        }
    } catch {
        # The result is supplementary to the on-screen error and exit code.
    }
}
if (-not $NoRestart) {
    if (-not $success) {
        $null = Add-Type -AssemblyName System.Windows.Forms
        $null = [Windows.Forms.MessageBox]::Show(
            "更新失败。$([Environment]::NewLine)$message", '断箭战前情报'
        )
    }
    if ($restartAllowed -and ($success -or $restored)) {
        try {
            $null = Start-Process -FilePath (Join-Path $Root 'BA-Scout.exe') -WorkingDirectory $Root -WindowStyle Normal
        } catch {
            $null = Add-Type -AssemblyName System.Windows.Forms
            $null = [Windows.Forms.MessageBox]::Show(
                "无法重新启动断箭战前情报。$([Environment]::NewLine)$($_.Exception.Message)$([Environment]::NewLine)备用文件：$backup",
                '断箭战前情报'
            )
            exit 2
        }
    }
}
if (-not $success) { exit 1 }
