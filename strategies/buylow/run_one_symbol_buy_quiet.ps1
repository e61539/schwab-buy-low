param(
  [string]$Title,
  [string]$Python,
  [string]$Script,
  [string]$Symbol,
  [double]$Usd,
  [Nullable[Double]]$AtrK,              # <- now optional
  [string]$OrderStyle = "limit",
  [double]$MaxSlippage = 0.003,
  [string]$Tz = "America/Detroit",
  [ValidateSet("regular","extended")] [string]$Hours = "regular",
  [string]$LogDir = "",
  [string]$ExtraArgs = ""               # e.g. "--soft-brake 8 --hard-brake 15 --brake-verbose --confirm"
)

# Phase 2 path hardening: resolve this wrapper from its own location, not from
# the process current directory.
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')

# Helper: split ExtraArgs but keep quoted chunks intact
function Split-Args([string]$s) {
  if (-not $s) { return @() }
  return [regex]::Matches($s,'(?:"([^"]*)"|(\S+))') | ForEach-Object {
    if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value }
  }
}

# Window title (best-effort)
try { $Host.UI.RawUI.WindowTitle = $Title } catch {}

# Resolve default logs under this repo/runtime.
if (-not $LogDir) {
  $LogDir = 'C:\temp\logs_ira1'
}

# Ensure log directory exists
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
$log = Join-Path $LogDir ("Buy_{0}_{1:yyyy-MM-dd_HHmmss}.log" -f $Symbol,(Get-Date))

# Sanity checks
if (-not (Test-Path $Script)) {
  "[ERR] Script not found: $Script" | Tee-Object -FilePath $log -Append
  exit 1
}

# Force unbuffered output & ignore SyntaxWarnings
$pyFlags = @('-u','-W','ignore::SyntaxWarning')

# Invariant numeric formatting
$usd = [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:0.##}", $Usd)
$slp = [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:0.###}", $MaxSlippage)

# Build arguments to the Python script
$argsList = @(
  $Script,
  '--symbols', $Symbol,
  '--usd-per-symbol', $usd,
  '--order-style', $OrderStyle,
  '--max-slippage', $slp,
  '--tz', $Tz,
  '--hours', $Hours,
  '--log-dir', $LogDir
)

# Only pass --atr-k if explicitly provided (lets atrk.json take precedence otherwise)
if ($AtrK.HasValue) {
  $atk = [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:0.##}", $AtrK.Value)
  $argsList += @('--atr-k', $atk)
}

# Append any extra flags (keep --confirm here if you want it; do NOT duplicate elsewhere)
$argsList += (Split-Args $ExtraArgs)

"[RUN] $Python $($pyFlags -join ' ') $($argsList -join ' ')" | Tee-Object -FilePath $log -Append

# Execute and mirror output to log
& "$Python" @pyFlags @argsList 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
"[EXIT] code=$code" | Tee-Object -FilePath $log -Append
exit $code
