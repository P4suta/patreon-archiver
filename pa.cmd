@echo off
docker.exe compose -f "%~dp0compose.yaml" run --rm pa %*
