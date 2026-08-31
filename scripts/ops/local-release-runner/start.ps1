$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $scriptDir "runner.py"
& python $runner @args
exit $LASTEXITCODE
