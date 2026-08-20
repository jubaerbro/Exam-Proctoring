<#
.SYNOPSIS
    Load a dotenv file into the current PowerShell session.

.DESCRIPTION
    The API reads configuration from the environment. `docker compose` does that
    for you from `.env`; a native process does not, so this loads a file into the
    shell you are standing in.

    Scope is deliberately 'Process': the variables die with this terminal. A
    'User' scope write would leave a database password in your Windows
    environment forever, which is not something a dev script should do to you.

    The VS Code launch configurations use `envFile` instead and do not need this.

.EXAMPLE
    .\scripts\load-env.ps1 .env.local
    .\scripts\load-env.ps1            # defaults to .env.local
#>
param(
    [string]$Path = ".env.local"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Path)) {
    Write-Error "No such file: $Path`nCopy .env.local.example to .env.local first (see RUNNING.md)."
}

$loaded = 0
Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }

    $split = $line.IndexOf("=")
    if ($split -lt 1) { return }

    $key = $line.Substring(0, $split).Trim()
    $value = $line.Substring($split + 1).Trim()

    # Strip a trailing `# comment`, but not a `#` inside quotes or a password.
    if ($value -notmatch '^["'']' -and $value -match '\s+#') {
        $value = ($value -split '\s+#')[0].Trim()
    }
    # Strip surrounding quotes if present.
    if ($value.Length -ge 2 -and
        (($value.StartsWith('"') -and $value.EndsWith('"')) -or
         ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    [Environment]::SetEnvironmentVariable($key, $value, "Process")
    $loaded++
}

Write-Host "Loaded $loaded variables from $Path into this session." -ForegroundColor Green
Write-Host "  DATABASE_URL = $($env:DATABASE_URL)" -ForegroundColor DarkGray
