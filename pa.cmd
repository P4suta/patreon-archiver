@echo off
REM patreon-archiver — Windows host trampoline.
REM
REM 1 行で Docker Compose に丸投げするだけ。compose.yaml がこの .cmd と
REM 同じディレクトリにある前提 (= 保存先 = repo ルート)。引数はそのまま
REM 中の `just` recipe に届く。
REM
REM Usage:
REM   pa.cmd                  -> just --list
REM   pa.cmd sync             -> mhtml/<latest>.mhtml で sync
REM   pa.cmd batch            -> urls.txt をまるごと
REM   pa.cmd download <URL>   -> 単発 download
REM   pa.cmd build            -> image を build (初回のみ)
docker.exe compose -f "%~dp0compose.yaml" run --rm pa %*
