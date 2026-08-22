# Lists Windows Task Scheduler jobs that likely run tender / icetrade parsers.
# Run on the server in PowerShell (admin not required): deploy\check-windows-scheduled-tasks-parser.ps1
#
# Look for duplicate schedules (e.g. angar_parser twice per night in addition to GitHub Actions).

$ErrorActionPreference = 'Continue'

$patterns = @(
    'tender_it',
    'it_parser',
    'angar_parser',
    'ParserAngar',
    'parserIT',
    'icetrade',
    'win-it-parser',
    'win-angar-parser'
)

function Test-ParserLikeAction {
    param(
        [string] $Execute,
        [string] $Arguments,
        [string] $WorkingDirectory
    )
    $blob = ($Execute + ' ' + $Arguments + ' ' + $WorkingDirectory).ToLowerInvariant()
    foreach ($p in $patterns) {
        if ($blob -match [regex]::Escape($p.ToLowerInvariant())) { return $true }
    }
    return $false
}

function Test-ParserLikeTaskName {
    param([string] $Name, [string] $Path)
    $blob = ($Name + ' ' + $Path).ToLowerInvariant()
    return ($blob -match 'parser|tender|icetrade|angar|it.?parser')
}

Write-Host '=== Scheduled tasks: actions matching parser / C:\tender_it ===' -ForegroundColor Cyan
$rows = @()
Get-ScheduledTask -ErrorAction SilentlyContinue | ForEach-Object {
    $task = $_
    foreach ($a in $task.Actions) {
        $ex = [string] $a.Execute
        $arg = [string] $a.Arguments
        $wd = [string] $a.WorkingDirectory
        if (Test-ParserLikeAction -Execute $ex -Arguments $arg -WorkingDirectory $wd) {
            $rows += [pscustomobject]@{
                TaskName  = $task.TaskName
                TaskPath  = $task.TaskPath
                State     = $task.State
                Execute   = $ex
                Arguments = $arg
                WorkDir   = $wd
            }
        }
    }
}
if ($rows.Count -eq 0) {
    Write-Host 'No tasks matched action paths/arguments (good if you rely only on GitHub Actions).' -ForegroundColor Green
} else {
    $rows | Sort-Object TaskPath, TaskName | Format-Table -AutoSize -Wrap
}

Write-Host "`n=== Scheduled tasks: name/path hint (parser / tender / icetrade) — review manually ===" -ForegroundColor Cyan
Get-ScheduledTask -ErrorAction SilentlyContinue |
    Where-Object { Test-ParserLikeTaskName -Name $_.TaskName -Path $_.TaskPath } |
    ForEach-Object {
        $task = $_
        $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue
        [pscustomobject]@{
            TaskName   = $task.TaskName
            TaskPath   = $task.TaskPath
            State      = $task.State
            LastRun    = $info.LastRunTime
            NextRun    = $info.NextRunTime
            LastResult = $info.LastTaskResult
        }
    } |
    Sort-Object TaskPath, TaskName |
    Format-Table -AutoSize

Write-Host "`n=== Tip ===" -ForegroundColor Yellow
Write-Host 'If you see overlapping angar/it_parser schedules with GitHub Actions, disable or delete the Task Scheduler entries to avoid duplicate Telegram messages.'
Write-Host 'Detail for one task: Get-ScheduledTask -TaskPath "\PATH\" -TaskName "NAME" | Get-ScheduledTaskInfo'
