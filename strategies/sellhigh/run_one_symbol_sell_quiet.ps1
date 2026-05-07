param(
  # ---- required core ----
  [Parameter(Mandatory = $true)]  [string]$Symbol,
  [Parameter(Mandatory = $true)]  [string]$Script,     # path to your sell_high script (e.g., sell_high_pct.py)
  [Parameter(Mandatory = $true)]  [string]$SellDic,
  [Parameter(Mandatory = $true)]  [string]$Tz,
  [Parameter(Mandatory = $true)]  [string]$Hours,
  [Parameter(Mandatory = $true)]  [string]$OnClose,
  [Parameter(Mandatory = $true)]  [string]$LogDir,

  # ---- optional / switches ----
  [string]$Python = "",                                 # will default to C:\python313\python.exe or 'python'
  [string]$Title,                                       # window title
  [switch]$Live,                                        # adds --confirm
  [switch]$Why,                                         # adds --verbose
  [switch]$NoPause,                                     # skip "Press Enter to close..." prompt
  [string]$ExtraArgs                                    # space-separated passthrough tokens
)

# ---------- strict errors early ----------
$ErrorActionPreference = 'Stop'

# ---------- sensible defaults / validation ----------
if (-not $Python -or $Python.Trim() -eq '') {
  if (Test-Path 'C:\python313\python.exe') { $Python = 'C:\python313\python.exe' } else { $Python = 'python' }
}

function _req([string]$name, [string]$val) {
  if (-not $val -or $val.Trim() -eq '') { throw "[ERR] -$name is required and was blank." }
}
_req 'Script'  $Script
_req 'SellDic' $SellDic
_req 'Tz'      $Tz
_req 'Hours'   $Hours
_req 'OnClose' $OnClose
_req 'LogDir'  $LogDir

if (-not (Test-Path $Script))  { throw "[ERR] Script not found: $Script" }
if (-not (Test-Path $SellDic)) { throw "[ERR] SellDic not found: $SellDic" }

# Optional: validate enum-y params to fail fast on typos
$validHours  = @('regular','extended')
$validOnClose = @('now','sleep','close','next')
if ($validHours  -notcontains $Hours)  { throw "[ERR] Hours must be one of: $($validHours -join ', ')." }
if ($validOnClose -notcontains $OnClose) { throw "[ERR] OnClose must be one of: $($validOnClose -join ', ')." }

# ---------- window title ----------
if ($Title) { try { $Host.UI.RawUI.WindowTitle = $Title } catch {} }

# ---------- logging ----------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp   = Get-Date -Format 'yyyyMMdd_HHmmss'
$logPath = Join-Path $LogDir ("sell_{0}_{1}.log" -f $Symbol, $stamp)

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[{0}] [INFO] Starting Sell-High for {1}. Log: {2}" -f $ts, $Symbol, $logPath | Tee-Object -FilePath $logPath -Append | Out-Null
"[{0}] [INFO] Args: Python='{1}' Script='{2}' tz={3} hours={4} onClose={5} sell_dic={6} Live={7} Why={8} Extra='{9}'" -f $ts, $Python, $Script, $Tz, $Hours, $OnClose, $SellDic, [bool]$Live, [bool]$Why, $ExtraArgs | Tee-Object -FilePath $logPath -Append | Out-Null

# ---------- build python args ----------
$pyFlags  = @('-u','-W','ignore::SyntaxWarning')  # keeps output unbuffered; hides noisy warnings
$argList  = @(
  $Script,
  '--symbols', $Symbol,
  '--sell-dic', $SellDic,
  '--tz',       $Tz,
  '--hours',    $Hours,
  '--on-close', $OnClose
)

if ($Live) { $argList += '--confirm' }
if ($Why)  { $argList += '--verbose' }

if ($ExtraArgs) {
  $tokens = ($ExtraArgs -split '\s+') | Where-Object { $_ -and $_.Trim() -ne '' }
  if ($tokens) { $argList += $tokens }
}

# ---------- exec ----------
$runPreview = ('{0} {1} {2}' -f $Python, ($pyFlags -join ' '), (($argList | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '))
"[{0}] [DBG] Exec: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $runPreview | Tee-Object -FilePath $logPath -Append | Out-Null

try {
  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  & $Python @pyFlags @argList 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $logPath -Append
  $exit = $LASTEXITCODE
} catch {
  $_ | Out-String | Tee-Object -FilePath $logPath -Append | Out-Null
  $exit = 1
} finally {
  if ($oldErrorActionPreference) { $ErrorActionPreference = $oldErrorActionPreference }
}

"[{0}] [INFO] Exit code: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $exit | Tee-Object -FilePath $logPath -Append | Out-Null

# ---------- optional pause for interactive runs ----------
if ((-not $NoPause) -or $exit -ne 0) {
  try { Read-Host '--- Press Enter to close this window ---' | Out-Null } catch {}
}

exit $exit
