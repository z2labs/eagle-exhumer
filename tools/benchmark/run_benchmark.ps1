<#
  run_benchmark.ps1 - Eagle Exhumer benchmark: convert + strong quality control on every design
  of the given corpus roots, manufacturing files compared with the reference library.

  -Corpus      folders with EAGLE designs (one sub-folder per design)
  -MfgRoot     reference library (eagle-exhumer-testdata\eagle77_cam); factory files next to a
               design are always preferred
  -Priority    Idle | BelowNormal | Normal | High (whole process tree; other work on the machine first)
  -Logs        log folder
  -Plugin      installed plugin folder (default: KiCad 10 3rd-party plugin location)
#>
param(
  [Parameter(Mandatory)][string[]]$Corpus,
  [string]$MfgRoot = '',
  [ValidateSet('Idle','BelowNormal','Normal','High')][string]$Priority = 'BelowNormal',
  [string]$Logs = "$PSScriptRoot\logs",
  [string]$Plugin = "$env:USERPROFILE\Documents\KiCad\10.0\3rdparty\plugins\com_github_z2labs_eagle-exhumer",
  [string]$Python = 'C:\Program Files\KiCad\10.0\bin\python.exe'
)
New-Item -ItemType Directory -Force $Logs | Out-Null
if ($MfgRoot) { $env:EAGLE_EXHUMER_MFG_ROOT = $MfgRoot }
$batch = Join-Path $Plugin 'eagle2kicad_batch.py'
foreach ($c in $Corpus) {
  $name = Split-Path $c -Leaf
  Write-Host "== $c"
  $p = Start-Process -FilePath $Python -ArgumentList '-u', "`"$batch`"", '.', '--force' -WorkingDirectory $c `
       -RedirectStandardOutput "$Logs\$name.log" -RedirectStandardError "$Logs\$name.err" -WindowStyle Minimized -PassThru
  $p.PriorityClass = $Priority          # children (KiCad, kicad-cli, the fixer) inherit it
  $p.WaitForExit()
}
& $Python "$PSScriptRoot\summarize.py" @Corpus --md "$Logs\summary.md"
