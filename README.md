# patreon-archiver

Patreon クリエイターが独自ドメイン経由(Cloudflare Stream 前段)で配信している
動画を、個人のオフライン視聴用にローカル保存するための Docker ベース環境。

ホスト側に必要なのは **Docker のみ**。yt-dlp / ffmpeg / Python / uv / just は
すべてコンテナに同梱されてて、Docker Compose 越しに `pa <subcmd>` でアクセスする。

**動作モデル**: **保存先のディレクトリに repo そのものを clone する**。ただし
**user の作業ファイルは全部 `data/` 配下に隔離される**。コンテナにマウント
されるのは `<repo>/data/` だけで、 `Dockerfile` / `scripts/` / `config/` のような
リポジトリのソースとは混ざらない。

```
<保存先>/                ← git clone 先
├── pa.cmd / compose.yaml / Dockerfile / scripts/ / ...   ← repo source (触らない)
└── data/                 ← user データだけ。`/data` にマウントされる
    ├── mhtml/            ← MHTML スナップショット (ぽち〜入力)
    ├── <uploader>/<日付>_<title>.mp4
    ├── archive.txt / seen_posts.txt / coverage.txt / urls.txt
    ├── cookies.txt (任意)
    ├── posts.md (inventory 出力)
    └── .retest/<ts>/...  ← `pa retest` の隔離 sandbox
```

Windows 側からは `pa.cmd` をダブルクリックするだけでコンテナが立ち上がる。

## 必要なもの

- Docker Desktop (Windows) もしくは Docker Engine 24+ (Linux)
- 以上

## 初回セットアップ

保存したい場所(例: `C:\Users\<you>\Downloads\patreon`)に repo を clone する。
複数の作家を分ける場合はクリエイターごとに別の場所に clone する。

```powershell
# Windows PowerShell
cd C:\Users\<you>\Downloads
git clone <url> patreon
cd patreon

# image build (初回のみ; 以降は image を共有)
.\pa.cmd build

# 環境スモーク (ネット不要)
.\pa.cmd smoke
```

WSL から開発する場合は同じ場所で:

```bash
cd /mnt/c/Users/<you>/Downloads/patreon
docker compose run --rm pa smoke
# または alias で:
alias pa='docker compose -f $PWD/compose.yaml run --rm pa'
```

`pa.cmd smoke` が通れば環境は OK。

## 「ぽち～」運用 (Windows)

`pa.cmd` は引数を取らずにダブルクリックで `just --list` を表示する。日常
運用は MHTML を 1 個投げ込んでショートカットを叩くだけ:

1. **MHTML を保存先の `data\mhtml\` に置く**
   Patreon の "posts" ページを Chrome / Edge で全件読み込んだ状態で
   「Web ページを単一ファイルで保存」→ `<保存先>\data\mhtml\<creator>.mhtml`
   として保存(または Downloads から移動)。複数置いても OK、`pa sync` は
   mtime 最新を自動で選ぶ。
2. **`sync.lnk` をダブルクリック**
   ショートカットの作り方:
   - 右クリック → 新規作成 → ショートカット
   - リンク先: `C:\Windows\System32\cmd.exe /c "<保存先>\pa.cmd" sync`
   - 作業フォルダ: `<保存先>`
   - 名前: `sync`
   ダブルクリックで `data\mhtml\<latest>.mhtml` に対する差分 sync が走る。

`batch` / `inventory` / `sync-dry` も同様の `.lnk` を作っておくと便利。
`.lnk` 自体は Windows のバイナリでリポジトリには含めない(各 user 環境で作る)。

## 使い方

保存先(= repo ルート)に居るのが前提。Windows なら `pa.cmd <subcmd>`、
WSL なら alias 経由の `pa <subcmd>`。

```powershell
cd <保存先>
.\pa.cmd download "https://stream.example.com/<日付>_<slug>_<token>/"
```

これだけで `<保存先>\data\<uploader>\<日付>_<title>.mp4` に保存される。
`archive.txt`(yt-dlp の重複検知用)も `<保存先>\data\` 直下に作られる。

### `pa` リファレンス

```text
pa                         # available recipes 一覧
pa sync [mhtml]            # MHTML 差分 sync(引数なしなら data/mhtml/<latest>)
pa sync-dry [mhtml]        # 差分 sync の "テストラン"(完全 read-only)
pa simulate <URL> [...]    # 1 URL を yt-dlp --simulate で検証(state 触らず)
pa retest <URL> [...]      # 強制再 download → /data/.retest/<ts>/(state 触らず)
pa download <URL>          # 単発 download
pa batch                   # urls.txt をまるっと
pa inventory [mhtml]       # MHTML → markdown 一覧 (引数なしで data/mhtml/<latest>)
pa resolve <URL>           # 配信 URL → CF Stream iframe URL を表示
pa shell                   # 中で対話 bash (cwd = /data = 保存先)
pa version                 # bundled yt-dlp version
pa build                   # image を build (Dockerfile 変更時)
```

### テストラン (state を一切触らない)

実 download や `seen_posts.txt` / `coverage.txt` の更新を伴わずに「次に sync を回したら
何が起きるか」を確認するには 2 段階用意してある:

| コマンド | ネットワーク | yt-dlp | mp4 出力 | 状態ファイル | 用途 |
|---|---|---|---|---|---|
| `pa sync-dry` | 呼ばない | 呼ばない | 無し | 完全に touch しない | URL list と coverage 計算だけ見る。何回でも叩ける |
| `pa simulate <URL>` | 叩く | `--simulate` で metadata 取得まで | 無し | `archive.txt` 更新なし | URL resolve / cookies / format 選択を end-to-end で確認 |
| `pa retest <URL>` | 叩く | 実 download まで走る | `.retest/<ts>/<uploader>/<file>.mp4` | `archive.txt` 読まないし書かない | 「もう archive 済みだけど実際に DL し直して動作確認」用。何回も叩ける |

```powershell
.\pa.cmd sync-dry         # data/mhtml/<latest> を読んで、何が download されるか報告
.\pa.cmd simulate "https://stream.example.com/<日付>_<slug>_<token>/"
.\pa.cmd retest    "https://stream.example.com/<日付>_<slug>_<token>/"
```

`sync-dry.lnk` をダブルクリックすれば `cmd.exe /k` 経由で結果が表示されたまま window が
残るので、安心して何度でも回せる。`retest` の出力は `.retest\<タイムスタンプ>\<uploader>\<title>.mp4`
に隔離されるので、本物の `<uploader>\` ツリーを上書きしない。確認後に
`Remove-Item -Recurse .retest` で全部きれいに消せる。

### Cookie 経由

トークン付き URL なら基本 Cookie 不要。401/403 を踏んだら:

1. Firefox/Chrome の cookies.txt 拡張で配信ページの Cookie を Netscape 形式で export
2. 保存先 (= repo ルート) に `cookies.txt` として置く

`compose.yaml` が `YTDLP_COOKIES=/data/cookies.txt` を常時セットしている
ので、ファイルがあれば yt-dlp が拾う、無ければ Cookie なし経路で動く。

### バッチ

`data\urls.txt` を編集して `pa batch`(`urls.txt.example` は repo ルートに置いてある):

```powershell
copy urls.txt.example data\urls.txt
notepad data\urls.txt
.\pa.cmd batch
```

`yt-dlp.conf` の `--download-archive` 設定により、すでに落とした URL は
自動的にスキップされる(`archive.txt` が状態ファイル、`data\` 直下に同居)。

### Patreon ページの inventory(俺的リスト化)

Patreon の "posts" ページをブラウザで全ポスト読み込んだ状態で MHTML
保存(Chrome / Edge: 右クリック → 「Web ページを単一ファイルで保存」)
→ `data\mhtml\` に置く。inventory にかけると 1 ポスト = 1 セクションの
markdown が出る。タイトル / 日付 / 動画長 / Patreon post URL の見出しの
下に、`# title: / # uploader: / # date: / # post:` のメタデータコメント
付きの **コピペ用 URL ブロック**(fenced code block)が並ぶ。欲しい
動画のブロックの中身を丸ごと `data\urls.txt` にコピーして `pa batch`。

```powershell
.\pa.cmd inventory > data\posts.md
```

引数なしなら `data/mhtml/<latest>.mhtml` を読む。特定ファイルを指定したい
ときは `pa.cmd inventory mhtml/specific.mhtml`(コンテナから見たパス、`data/` 直下からの相対)。

メタデータコメントは `download.py` が読み取って `--parse-metadata` で
yt-dlp に注入される。Cloudflare Stream extractor は uploader/title/日付を
返さないので、これがないとファイル名が `NA/NA_<hex>_[<hex>].mp4` に
なる(参考: 該当 issue は `scripts/download.py` の冒頭コメント)。

非動画 post(selfie / wallpaper / お知らせ)は stream URL なしで出るので
すぐ識別できる。

### 差分 sync(`pa sync`)

新しい post だけを拾いたい場合は MHTML を `data\mhtml\` に置いて:

```powershell
.\pa.cmd sync
```

これは `inventory.py --seen-file /data/seen_posts.txt --minimal` の出力を
`/data/urls.txt` に書き、URL が 1 本でもあれば batch を起動、終了後に
ハンドルした post の Patreon canonical URL を `/data/seen_posts.txt` に
append する(`sort -u` で dedup)。MHTML を週次で `data\mhtml\` に上書き保存
→ `pa sync` を回すだけで新規 post が自動で降ってくる。

#### Gap detection(`coverage.txt`)

MHTML は **完全である必要はない** — 初期表示の 10〜20 件だけで十分。`sync`
は `coverage.txt` に **anchor 日**(= 直近の "good sync" 時の最新
post 日)を持たせて gap を判定する:

- **MHTML 最古日 ≤ anchor**: MHTML が anchor まで届いてる ⇒ 連続性 OK ⇒
  anchor を MHTML 最新日まで前進
- **MHTML 最古日 > anchor**: anchor まで届いてない ⇒ `(anchor, MHTML 最古]`
  の窓に未処理 post があるかも ⇒ **anchor は据え置き**、可視範囲の新規分
  だけ download、毎回 sync 末尾で warning を出す

```
[sync] gap pending — dates (2026-01-15, 2026-04-23) may have un-handled
       posts. Visible-page diff is being downloaded; the system keeps
       the gap pending until a future MHTML reaches back to 2026-01-15
       or earlier.
```

anchor は **gap が埋まるまで動かない** ので、システムが「いつか深い MHTML
が来るのを黙って待つ」状態になる。次に十分深い MHTML を投げたら anchor は
最新日まで一気に前進、warning も自動的に止む。

初回は coverage.txt が無くても sync が `[sync] coverage anchor initialized
at YYYY-MM-DD (first sync).` と言いながら自動 init するので、手動で
セットアップする必要は無い。

初回はまず `seen_posts.txt` を **現状の MHTML** で seed しておくと
「新規分のみ」運用が始められる(`coverage.txt` は次回 `pa sync` 実行時に
自動 init される):

```bash
# WSL: data/mhtml/ に snapshot.mhtml を置いた状態で
pa inventory --minimal \
  | grep '^# post: ' | sed 's/^# post: //' | sort -u \
  > data/seen_posts.txt
```

## 保存先を切り替える (外付け SSD / 別の作品ごと)

**保存先ごとに repo を別 clone する**。`compose.yaml` が「自分のいる
ディレクトリ」を `/data` にマウントするので、clone した場所そのものが
保存先になる:

```powershell
cd F:\Creator-A
git clone <url> .
.\pa.cmd build      # 同 image を流用するなら省略可
.\pa.cmd sync       # F:\Creator-A\mhtml\ から拾う

cd F:\Creator-B
git clone <url> .
.\pa.cmd sync
```

`archive.txt` / `seen_posts.txt` / `coverage.txt` はそれぞれの保存先に
独立して存在するので、作家ごとの状態が混ざらない。image (`patreon-
archiver:local`) は OS に 1 個だけ build しておけば全 clone で共有される。

## 環境変数(任意 tunables)

`compose.yaml` が default を渡すので shell からの export は不要。変えたい
ときだけ shell でセットしてから `pa.cmd` を呼ぶ(または保存先の `.env`
ファイルに書く — Compose が自動 load する)。

| 変数 | 既定 | 用途 |
|---|---|---|
| `TZ` | `Asia/Tokyo` | コンテナ内の TZ |
| `YTDLP_BATCH_SLEEP_MIN` | `5` | batch / sync の動画間 sleep 下限(秒) |
| `YTDLP_BATCH_SLEEP_MAX` | `15` | 同上限 |

ある一回だけ動作を変えたい場合は CLI 側で yt-dlp フラグを上書き可能:

```powershell
.\pa.cmd download "<URL>" --limit-rate 2M --concurrent-fragments 1
```

(yt-dlp は同じ flag が複数あれば後勝ち)

## ダウンロード中の見え方

`docker compose run --rm` がデフォルトで TTY を attach するので、yt-dlp の
**進捗バーはターミナルにそのまま in-place で更新される**。
`--progress-delta 5` で更新頻度を 5 秒に絞ってあるので、TTY でも piped でも
騒がしくならない。

```
[start] Example Creator — Sample post title (id=abc123...)
[download]  47.2% of   1.45GiB at  9.87MiB/s ETA 02:14 (frag 87/372)
[batch] sleeping 12s before next URL
[start] Example Creator — Sample bonus post (id=def456...)
```

- パーセンテージ / 残時間 / 速度 / fragment カウンタは 5 秒間隔で更新
- 動画の開始時 (`[start] ...`) と完了時 (`[done] ... -> /data/...`) を
  改行で残すので、ログをスクロールバックすると履歴が読める
- バッチ中の動画間 sleep は `[batch] sleeping Ns before next URL` で見える
  (`download.py` 側で実装。`YTDLP_BATCH_SLEEP_MIN`/`MAX` で範囲調整)
- ログファイルにも残したい場合は `2>&1 | tee batch.log`。`--progress-delta`
  のおかげでログ量は妥当に収まる

## 速度・負荷について

CDN 視点で「scraper っぽい」シグナル(同一 IP からの大量並列接続、メタ情報の連打、
人間っぽくないタイミング)を出さなければ、native 速度で 1 本ずつ引いていく分には
1 視聴者と区別がつかない。なので default は **「並列を絞る、メタは少し休ませる、
本体速度は抑えない」**。

| 設定 | 値 | 効果 |
|---|---|---|
| `--limit-rate` | (なし) | native 速度で download。CDN は単一視聴者の native 速度を問題視しない |
| `--concurrent-fragments` | `4` | yt-dlp の中庸値。ブラウザが CDN に張る並列接続より少ない |
| `--sleep-requests` | `1s` | extractor の HTTP 呼び出し間に 1 秒挿入 |
| `YTDLP_BATCH_SLEEP_MIN` / `MAX` | `5` / `15` 秒 (batch のみ) | 動画間に random sleep |
| `--retry-sleep extractor/http/fragment` | exp backoff 上限 30-60 秒 | 429 等を踏んだら自動で指数 backoff |

50 本 × 1 時間 HD(1 本 ≈ 3.5GB)を 10 MB/s で引くと、1 本 6 分 + 動画間 ≈ 10 秒で
合計 5 時間強。一晩で終わるオーダー。

帯域を意図的に絞りたい(あるいは更に並列を上げたい)場合は `config/yt-dlp.conf`
を編集。

## yt-dlp の月次更新

1. `gh release list -R yt-dlp/yt-dlp -L 3` で最新版を確認
2. `pyproject.toml` の `yt-dlp[default]==<X.Y.Z>` を書き換え
3. `.\pa.cmd build`(`docker compose build --no-cache --pull pa` 同等が必要なら
   `docker compose build --no-cache --pull pa` を直叩き)
4. `.\pa.cmd smoke` を確認

## ディレクトリ構成

```
<保存先>/                    # 保存先 = repo ルート (git clone 先)
│
│ # ── repo source (動かさない) ───────────────────────────────
├── compose.yaml             # docker compose service 定義
├── pa.cmd                   # Windows トランポリン (1 行)
├── Dockerfile               # python:3.13-slim + ffmpeg + yt-dlp + just via uv
├── justfile                 # in-container dispatcher (image にも同梱)
├── pyproject.toml / uv.lock # yt-dlp の version pin + 再現性ロック
├── urls.txt.example         # data/urls.txt の書式サンプル
├── config/yt-dlp.conf       # 共通フラグ
├── scripts/
│   ├── _mhtml.py            # `data/data/mhtml/<latest>` の自動検出
│   ├── download.py          # yt-dlp wrapper (メタ注入 + 1 URL 1 invocation)
│   ├── inventory.py         # MHTML → markdown / minimal blocks
│   ├── publish.py           # transactional publish helper
│   ├── resolve.py           # publisher URL → CF Stream iframe URL
│   ├── sync.py              # MHTML 差分 sync(seen + coverage)
│   └── smoke.sh             # offline 検証
│
│ # ── user データ (/data にマウントされる唯一のディレクトリ) ─
└── data/                    # 触る場所はここだけ。`.gitignore` で中身全 ignore
    ├── mhtml/               # ぽち～用 MHTML 入力
    │   └── <creator>.mhtml
    ├── <uploader>/          # 動画出力
    │   └── <date>_<title>.mp4
    ├── cookies.txt          # 任意
    ├── urls.txt             # batch 入力
    ├── seen_posts.txt       # sync 状態
    ├── coverage.txt         # sync 状態
    ├── archive.txt          # yt-dlp 状態
    ├── posts.md             # `pa inventory > posts.md` の出力
    └── .retest/             # `pa retest` の隔離 sandbox
        └── <ts>/<uploader>/<file>.mp4
```

実行時に `<保存先>/data/` だけが `/data` に bind mount される。
コンテナ内の repo source は image に焼き込まれた `/work/` 配下にあり、
host の `/data` に repo の Dockerfile や scripts が見えることは無い。

## 設計メモ

- 認証は presigned URL 前提で **Cookie なしから試す**。403/401 だけ Cookie 経路。
- ファイル名は **日本語そのまま** (`--restrict-filenames` は使わない)。
- HLS 想定で `--concurrent-fragments 4` と `--retry-sleep fragment:exp=1:60`。
- メタデータは inventory が emit するブロックから `download.py` が
  `--parse-metadata` で注入。CloudflareStream extractor は uploader/title/
  date を一切返さないので、wrapper 側で埋めないとファイル名が `NA` 連発になる。
- mp4 の `comment` タグには JWT iframe URL ではなく Patreon post URL を埋め込む
  (token expire しても永続的に追跡できる canonical reference)。
- サイドカー(`*.info.json` / `*.description` / `*.jpg`)は出さない方針。
  サムネは `--embed-thumbnail` で mp4 内に取り込んで完結させる。
- DRM はかかっていない前提。Widevine 等が出てきた場合このツールは扱わない。
- 保存先 = repo ルート。複数の保存先を切り替えるときは別の場所に
  もう一度 clone する。`compose.yaml` が「自分のいるディレクトリ」を
  `/data` にマウントするので、image は使い回せて state は保存先ごとに
  独立する。
- **transactional publish**: yt-dlp の出力は `/var/lib/pa/staging/<token>/`
  (コンテナ内の私用領域、bind mount **ではない**) に書き出す。merge / remux /
  thumbnail/metadata の embed が全部 staging 上で完了してから、`download.py`
  が完成した mp4 を `/data` に **atomic rename** で publish する。同時に
  `/data/archive.txt` への新エントリも tmp + rename で atomic 更新。
  - **保証**: `/data` には部分ファイルが一切現れない。Ctrl-C / OOM /
    container kill / ディスクフル — どのタイミングで死んでも、ユーザーから
    見える `/data` は「過去の完成済み mp4 だけ」のまま
  - publish 中の中継は `.pa-publish.<token>.tmp`(先頭ドット) で隠れる。
    cross-FS のコピー後に同 FS 内で `os.replace` するので最終 rename は atomic
  - 失敗した run は staging dir ごと破棄。`/data/archive.txt` も書き換わら
    ないので、次の run で同じ URL が自然に再 download される
  - 過去に crash で残ったかもしれない stale な `.pa-publish.*.tmp` は batch
    の冒頭で sweep する

## Development

`pa.cmd` (Windows) / `docker compose run --rm pa` (WSL) is enough to *use*
the tool. To *contribute*, you have two paths:

### A. Devcontainer(推奨)

`.devcontainer/devcontainer.json` が `Dockerfile` の `dev` ターゲットを
そのまま使う。VSCode / Cursor / `devcontainer` CLI のいずれでも開ける:

```bash
# CLI から
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . bash
```

VSCode なら "Dev Containers: Reopen in Container" を選ぶだけ。
コンテナ起動時に postCreateCommand が `uv sync --locked --group dev` と
`lefthook install` を流すので、ホストには **Docker 以外何も要らない**。

中で使えるもの:
- `uv run ruff check` / `uv run pyright` / `uv run pytest`(直接 `ruff` /
  `pyright` / `pytest` でも OK — `.venv/bin` が PATH 先頭)
- `lefthook` / `typos` / `hadolint` / `shellcheck` / `git`(全部 image に同梱)
- `just <recipe>` も使える(image は runtime 継承なので、コンテナ越しに
  叩く recipe 群がそのまま手元で動く)

`Dockerfile` の `runtime` ステージ(本番イメージ)と `dev` ステージ
(devcontainer)が同じツールチェーン上に乗るので、CI / 本番 / 手元の
ドリフトが起きにくい。

### B. ホストで直接

mise を持っていてコンテナを介したくない場合:

```bash
mise use -g uv@latest typos@latest lefthook@latest shellcheck@latest hadolint@latest
uv sync --group dev
lefthook install   # wires .git/hooks
```

Then iterate:

```bash
uv run ruff check         # lint
uv run ruff format        # format
uv run pyright            # strict type-check
uv run pytest             # tests + 100% branch coverage gate
typos                     # spell check
```

CI runs the same set on every PR via `.github/workflows/lint.yml`. Coverage
is gated at **100% branch** with **no `pragma: no cover` / `pragma: no
branch` suppressions allowed** — defensive branches must be exercised by
real tests (or the defensive code refactored away).

### Integration tests

`tests/test_integration.py` exercises the actual built `patreon-archiver:local`
image end-to-end (smoke / version / recipe list / sync-dry idempotency /
seen-posts seed filtering / multi-MHTML auto-detect / inventory) with
**no network access** — every test bind-mounts a tempdir as `/data` and
calls only recipes that stay local. The whole module auto-skips if
docker isn't running or the image isn't built, so:

```bash
uv run pytest                   # unit + integration (integration auto-skips if image absent)
uv run pytest tests/test_integration.py  # only the integration suite
```

Once `pa.cmd build` (or `docker compose build pa`) has produced the image
locally, every subsequent `uv run pytest` re-runs the integration shakedown
automatically. Treat it as the regression net for any change to
`compose.yaml` / `Dockerfile` / `justfile` / scripts that affects the
container surface.

Dependency updates are automated via Renovate (`renovate.json`):
- Python dev tooling: minor/patch auto-merged
- GitHub Actions: digest+minor/patch auto-merged
- yt-dlp + Dockerfile base: PR opened, manual review (extractor /
  base-image churn warrants a human eye)
- Weekly lockfile maintenance every Monday morning JST

## License

MIT — see [`LICENSE`](LICENSE). The license covers **this software**;
it does not grant any rights over content downloaded with it. The
copyright of any video, image, or other media retrieved through this
tool remains with its original creator and is governed by their terms.

## Disclaimer

This tool is for **personal, offline archiving** of content the user
already has paid, authorized access to (e.g. videos posted by a
Patreon creator the user actively supports as a paying subscriber).
It does **not** implement DRM bypass — Widevine, FairPlay, PlayReady
and similar protected streams are explicitly out of scope and will
not be handled. It does not facilitate piracy, large-scale scraping,
or redistribution of paid content.

By using this software you acknowledge that:

- **Compliance with applicable copyright law is your responsibility.**
  This includes (non-exhaustively) the U.S. DMCA / 17 U.S.C. § 1201,
  the EU Copyright Directive (2019/790), and Japan's 著作権法 — in
  particular the personal-use copying provisions (e.g. 第30条) and
  their limitations.
- **Compliance with the Terms of Service of every platform you access
  is your responsibility.** Patreon, Cloudflare Stream, and any
  publisher domain in front of them all have their own ToS that may
  restrict local archiving even by paying subscribers. Verify before
  you use.
- **You will not redistribute** archived content, in whole or in part,
  publicly or to third parties. Personal offline viewing and public
  republication are categorically different acts.
- **The authors disclaim all liability** for misuse of this software,
  to the extent permitted by law. This software is provided "as is",
  without warranty of any kind, per the MIT license.

このツールは、利用者本人が **正当に有料 subscribe している** Patreon
クリエイター(支援対象本人)の動画を **個人視聴用にローカル保存**する
目的で書かれている。DRM 回避は実装していない(Widevine 等の保護ストリームは
対応外)し、海賊行為・有料コンテンツの再配布・大規模スクレイピングを
補助するものでもない。

各国の著作権法(米 DMCA、EU Copyright Directive、日本の著作権法 — 特に
私的使用のための複製規定 第30条と例外)および各 platform の ToS(Patreon、
Cloudflare Stream、配信ドメイン側)への遵守はすべて **利用者の自己責任**。
取得したコンテンツの **再配布は明示的に禁止**(個人視聴と公開再頒布は
別物)。本ツール作者は誤用に対する一切の責任を負わない(MIT 規定の通り、
法律の許す範囲で)。
