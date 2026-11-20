[CmdletBinding()]
param([switch]$ResolveDependencies)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$module = Join-Path $project 'apps/supportflow/spring-sandbox'
$cache = Join-Path $project '.local/spring-sandbox-build'
$version = '3.9.11'
$expected = '03e2d65d4483a3396980629f260e25cac0d8b6f7f2791e4dc20bc83f9514db8d0f05b0479e699a5f34679250c49c8e52e961262ded468a20de0be254d8207076'
$zip = Join-Path $cache "apache-maven-$version-bin.zip"
$maven = Join-Path $cache "apache-maven-$version/bin/mvn.cmd"
$url = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$version/apache-maven-$version-bin.zip"
New-Item -ItemType Directory -Force -Path $cache | Out-Null
if (!(Test-Path -LiteralPath $zip)) {
    $download = "$zip.$([guid]::NewGuid().ToString('N')).part"
    Invoke-WebRequest -Uri $url -OutFile $download -TimeoutSec 90
    if ((Get-FileHash -LiteralPath $download -Algorithm SHA512).Hash.ToLowerInvariant() -cne $expected) {
        throw 'Maven archive checksum mismatch.'
    }
    Move-Item -LiteralPath $download -Destination $zip
}
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA512).Hash.ToLowerInvariant() -cne $expected) {
    throw 'Cached Maven archive checksum mismatch.'
}
if (!(Test-Path -LiteralPath $maven)) { Expand-Archive -LiteralPath $zip -DestinationPath $cache }
$mavenArgs = @('--batch-mode','--no-transfer-progress','--strict-checksums','-Dstyle.color=never',"-Dmaven.repo.local=$cache/repository",'-f',"$module/pom.xml")
$dependencyGoal = 'org.apache.maven.plugins:maven-dependency-plugin:3.8.1:tree'
if ($ResolveDependencies) {
    & $maven @mavenArgs $dependencyGoal "-DoutputFile=$cache/dependency-tree.txt"
    exit $LASTEXITCODE
}
& $maven @mavenArgs clean verify $dependencyGoal "-DoutputFile=$cache/dependency-tree.txt"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$lock = Join-Path $module 'dependency-tree.lock'
if (!(Test-Path -LiteralPath $lock)) { throw "Dependency lock missing; review $cache/dependency-tree.txt" }
if (((Get-Content -LiteralPath $lock) -join "`n") -cne ((Get-Content -LiteralPath "$cache/dependency-tree.txt") -join "`n")) {
    throw "Resolved Maven dependencies differ from $lock"
}
