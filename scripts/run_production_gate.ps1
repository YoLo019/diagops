$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$composeFile = Join-Path $PSScriptRoot "..\compose.production-gate.yaml"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runId = "run-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ")
$runRoot = Join-Path $PSScriptRoot "..\output\production-acceptance\$runId"
$scenarios = @(
    "deployment_regression",
    "dependency_timeout",
    "traffic_spike",
    "healthy_control",
    "memory_pressure",
    "network_corruption",
    "process_failure"
)
$scenarioResults = @()

docker version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker daemon is unavailable"
}

New-Item -ItemType Directory -Path $runRoot -Force | Out-Null

foreach ($scenario in $scenarios) {
    $project = "diagops-v10-" + $scenario.Replace("_", "-")
    $scenarioRoot = Join-Path $runRoot $scenario
    New-Item -ItemType Directory -Path $scenarioRoot -Force | Out-Null
    $env:DIAGOPS_PRODUCTION_SCENARIO = $scenario
    $env:DIAGOPS_PRODUCTION_OUTPUT = (Resolve-Path $scenarioRoot).Path

    docker compose -f $composeFile -p $project down -v --remove-orphans
    $composeExit = 1
    try {
        docker compose -f $composeFile -p $project up `
            --build `
            --abort-on-container-exit `
            --exit-code-from scenario-runner
        $composeExit = $LASTEXITCODE
    }
    finally {
        docker compose -f $composeFile -p $project down -v --remove-orphans
    }

    $scenarioResult = Join-Path $scenarioRoot "scenario.json"
    if (-not (Test-Path -LiteralPath $scenarioResult -PathType Leaf)) {
        throw "Production scenario did not produce an artifact: $scenario"
    }
    $scenarioResults += $scenarioResult
    if ($composeExit -ne 0) {
        Write-Warning "Production scenario failed: $scenario"
    }
}

$resultPath = Join-Path $runRoot "result.json"
uv run --project $repoRoot python -m backend.services.production_acceptance aggregate `
    @scenarioResults `
    --output $resultPath
if ($LASTEXITCODE -ne 0) {
    throw "Production acceptance Gate failed; artifact preserved at $resultPath"
}

Write-Output $resultPath
