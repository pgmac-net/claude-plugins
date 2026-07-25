#!/usr/bin/env pwsh
param(
    [switch]$Help,
    [switch]$Uninstall
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Get-Item $ScriptDir).Parent.FullName
$SkillsSourceDir = Join-Path $RepoRoot "plugins" "pgmac-workflows" "skills"
$SkillsTargetDir = Join-Path $env:USERPROFILE ".config" "opencode" "skills"

$ScriptName = Split-Path -Leaf $PSCommandPath

$SkillNames = @(
    "create-pir"
    "pickup-ticket"
    "grilling"
    "domain-modeling"
)

function Show-Help {
    @"
Usage: $ScriptName [OPTION]

Install or uninstall opencode skills from this repository.

Options:
  -Uninstall    Remove symlinked skills from the global skills directory
  -Help         Show this help message

Without options, installs skills to:
  $SkillsTargetDir
"@
    exit 0
}

function Uninstall-Skills {
    $count = 0
    foreach ($name in $SkillNames) {
        $target = Join-Path $SkillsTargetDir $name
        if (Test-Path $target) {
            Remove-Item -Recurse -Force $target
            Write-Host "Removed: $target"
            $count++
        }
    }
    if ($count -eq 0) {
        Write-Host "No installed opencode skills found."
    } else {
        Write-Host "Uninstalled $count skill(s). Restart opencode for the change to take effect."
    }
    exit 0
}

# Parse arguments
if ($Help) { Show-Help }
if ($Uninstall) { Uninstall-Skills }

# Check for Administrator or Developer Mode
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "Windows symbolic link creation typically requires Administrator privileges or Developer Mode enabled."
    Write-Warning "If the script fails, either:"
    Write-Warning "  1. Run PowerShell as Administrator, or"
    Write-Warning "  2. Enable Developer Mode (Settings > Privacy & Security > For Developers > Developer Mode)"
    Write-Host ""
}

# Verify source directories exist
foreach ($name in $SkillNames) {
    $source = Join-Path $SkillsSourceDir $name
    if (-not (Test-Path $source)) {
        Write-Error "Source skill directory not found: $source"
        exit 1
    }
}

# Create target directory
New-Item -ItemType Directory -Force -Path $SkillsTargetDir | Out-Null

# Install each skill
$count = 0
foreach ($name in $SkillNames) {
    $source = Join-Path $SkillsSourceDir $name
    $target = Join-Path $SkillsTargetDir $name

    # Check for existing path
    $item = Get-Item $target -ErrorAction SilentlyContinue -Force
    if ($item) {
        if ($item.LinkType -eq "SymbolicLink") {
            Remove-Item -Force $target
        } else {
            Write-Warning "$target exists and is not a symlink. Skipping $name."
            continue
        }
    }

    New-Item -ItemType SymbolicLink -Path $target -Target $source | Out-Null
    Write-Host "Installed: $name"
    $count++
}

Write-Host ""
Write-Host "Installed $count skill(s) to $SkillsTargetDir"
Write-Host "Restart opencode for the skills to become available."
