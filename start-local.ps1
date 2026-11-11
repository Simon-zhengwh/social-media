param(
    [switch]$Check,
    [switch]$Recover,
    [ValidateRange(1, 65535)][int]$BackendPort = 8000,
    [ValidateRange(1, 65535)][int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'
$backendDir = Join-Path $PSScriptRoot 'backend'
$pythonPath = Join-Path $backendDir '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Missing backend/.venv. Follow the Python setup in README.md first.'
}

$launchArgs = @('-X', 'utf8', '-m', 'scripts.local_dev', '--backend-port', "$BackendPort", '--frontend-port', "$FrontendPort")
if ($Check) { $launchArgs += '--check' }
if ($Recover) { $launchArgs += '--recover' }

Push-Location -LiteralPath $backendDir
try {
    & $pythonPath @launchArgs
    $launchExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $launchExitCode
