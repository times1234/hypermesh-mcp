$ErrorActionPreference = "SilentlyContinue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $ProjectDir "hypermesh_batch_path.txt"

function Test-HmBatchPath {
    param([string]$PathValue)
    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return $false
    }
    return (Test-Path -LiteralPath $PathValue -PathType Leaf)
}

$candidates = New-Object System.Collections.Generic.List[string]

if ($env:HYPERMESH_BATCH_EXE) {
    $candidates.Add($env:HYPERMESH_BATCH_EXE)
}

$versions = @("2026", "2025", "2024", "2023", "2022", "2021", "2020", "2019")
$drives = @("C:", "D:", "E:", "F:")
$subDirs = @(
    "Program Files\Altair",
    "Program Files (x86)\Altair"
)
$hmBins = @(
    "hwdesktop\hm\bin\win64\hmbatch.exe",
    "hwdesktop\hw\bin\win64\hmbatch.exe"
)

foreach ($drive in $drives) {
    foreach ($subDir in $subDirs) {
        foreach ($version in $versions) {
            foreach ($hmBin in $hmBins) {
                $candidates.Add((Join-Path "$drive\" (Join-Path $subDir (Join-Path $version $hmBin))))
            }
        }
    }
}

$found = $null
foreach ($candidate in $candidates) {
    if (Test-HmBatchPath $candidate) {
        $found = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}

if (-not $found) {
    Write-Host "Common paths did not contain hmbatch.exe."
    Write-Host "Searching Altair folders. This may take a moment..."
    foreach ($drive in $drives) {
        foreach ($subDir in $subDirs) {
            $root = Join-Path "$drive\" $subDir
            if (-not (Test-Path -LiteralPath $root -PathType Container)) {
                continue
            }
            $match = Get-ChildItem -LiteralPath $root -Filter "hmbatch.exe" -Recurse -File -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($match) {
                $found = $match.FullName
                break
            }
        }
        if ($found) {
            break
        }
    }
}

if (-not $found) {
    Write-Host "ERROR: hmbatch.exe was not found."
    Write-Host "Please locate hmbatch.exe manually, then put its full path into:"
    Write-Host "  $ConfigFile"
    exit 1
}

[System.IO.File]::WriteAllText($ConfigFile, $found + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

Write-Host "Found hmbatch.exe:"
Write-Host "  $found"
Write-Host ""
Write-Host "Wrote project config:"
Write-Host "  $ConfigFile"

try {
    setx HYPERMESH_BATCH_EXE "$found" | Out-Null
    Write-Host ""
    Write-Host "Set user environment variable HYPERMESH_BATCH_EXE."
} catch {
    Write-Host ""
    Write-Host "WARNING: Failed to set user environment variable HYPERMESH_BATCH_EXE."
    Write-Host "The project config file was written, so this project can still use hmbatch.exe."
}

exit 0
