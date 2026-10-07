<#
Build what renderdoc-mcp needs from a RenderDoc checkout (x64 Release, static CRT):
  x64\Release\renderdoc.dll, renderdoccmd.exe, pymodules\renderdoc.pyd (for Python 3.<minor>)

  powershell -ExecutionPolicy Bypass -File build_renderdoc.ps1 [-RenderDocRoot D:\src\renderdoc] [-PythonMinor 10] [-Rebuild]

RenderDocRoot defaults to ..\renderdoc (this repo next to the checkout), then .. (inside it).
Get the source with: git clone https://github.com/baldurk/renderdoc.git

Notes
- Builds the solution target Utility\renderdoccmd (pulls renderdoc.dll, drivers, breakpad in
  the solution's dependency order; building renderdoc.vcxproj alone misses breakpad libs).
- The Qt UI (qrenderdoc) is not built.
- renderdoc.pyd links one pythonXY.dll. RenderDoc's bundled Python is 3.6, so it is rebuilt
  against a local Python via VSPythonOverridePath (needs include\, libs\pythonXY.lib and a
  pythonXY.zip, which only has to exist).
- Close anything that has renderdoc.dll loaded (the MCP server, Python sessions) first, or the
  link step fails on a locked file.
#>
param(
    [string]$RenderDocRoot = '',
    [int]$PythonMinor = 10,
    [switch]$Rebuild
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RenderDocRoot) {
    $sibling = Join-Path (Split-Path -Parent $here) 'renderdoc'
    if (Test-Path (Join-Path $sibling 'renderdoc.sln')) { $RenderDocRoot = $sibling }
    else { $RenderDocRoot = Split-Path -Parent $here }
}
$root = (Resolve-Path $RenderDocRoot).Path.TrimEnd('\')
$sln = Join-Path $root 'renderdoc.sln'
if (-not (Test-Path $sln)) { throw "renderdoc.sln not found at $sln (pass -RenderDocRoot)" }
Write-Host "RenderDoc source: $root"

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1
if (-not $msbuild) { throw 'MSBuild not found (install Visual Studio with the C++ workload)' }
Write-Host "MSBuild: $msbuild"

# --- stage a minimal Python tree for VSPythonOverridePath ---
$ver = "3.$PythonMinor"
$pyBase = (& py "-$ver" -c 'import sys; print(sys.base_prefix)').Trim()
if (-not $pyBase) { throw "Python $ver not found (py -$ver)" }
$tag = "3$PythonMinor"
$stage = Join-Path $here ".build\py$tag"
New-Item -ItemType Directory -Force (Join-Path $stage 'libs') | Out-Null
Copy-Item (Join-Path $pyBase 'include') $stage -Recurse -Force
Copy-Item (Join-Path $pyBase "libs\python$tag.lib") (Join-Path $stage 'libs') -Force
& py "-$ver" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1],'w').close()" (Join-Path $stage "python$tag.zip")
Write-Host "Python $ver from $pyBase staged at $stage"

$props = Join-Path $here 'static_crt.props'
$common = @('-p:Configuration=Release', '-p:Platform=x64', "-p:ForceImportBeforeCppTargets=$props",
            '-m', '-nologo', '-v:minimal', '-clp:ErrorsOnly;Summary')
$suffix = ''
if ($Rebuild) { $suffix = ':Rebuild' }

Write-Host '== renderdoc.dll + renderdoccmd (static CRT)'
& $msbuild $sln "-t:Utility\renderdoccmd$suffix" @common
if ($LASTEXITCODE -ne 0) { throw "renderdoccmd build failed ($LASTEXITCODE)" }

Write-Host "== renderdoc.pyd for Python $ver"
$env:VSPythonOverridePath = $stage
$pyproj = Join-Path $root 'qrenderdoc\Code\pyrenderdoc\pyrenderdoc_module.vcxproj'
# the module's intermediate files don't track the Python version: always rebuild it
& $msbuild $pyproj '-t:Rebuild' '-p:BuildProjectReferences=false' "-p:SolutionDir=$root\" @common
if ($LASTEXITCODE -ne 0) { throw "pyrenderdoc_module build failed ($LASTEXITCODE)" }

$out = Join-Path $root 'x64\Release'
$check = "import os,sys; os.add_dll_directory(r'$out'); sys.path.insert(0, r'$out\pymodules'); import renderdoc as rd; print('renderdoc', rd.GetVersionString(), 'loads in Python', sys.version.split()[0])"
& py "-$ver" -c $check
if ($LASTEXITCODE -ne 0) { throw 'renderdoc.pyd failed to import' }
Write-Host "OK: $out"
