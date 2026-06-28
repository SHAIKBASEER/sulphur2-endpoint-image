param(
  [Parameter(Mandatory = $true)]
  [string]$Image,

  [string]$Tag = "latest"
)

$ErrorActionPreference = "Stop"

$FullImage = "${Image}:${Tag}"

docker build -t $FullImage .
docker push $FullImage

Write-Host "Pushed $FullImage"
