# patreon-archiver

支援している Patreon クリエイターが独自ドメイン経由で配信している動画を、
個人のオフライン視聴用にローカル保存するための Docker ベース環境。

ホスト側に必要なのは **Docker** と **just** だけ。yt-dlp / ffmpeg / Python /
uv はすべてコンテナ内に閉じ込めてある。

## 必要なもの

- Docker Engine 24+ と `docker compose` plugin
- [`just`](https://github.com/casey/just)
- (任意) [`jj`](https://docs.jj-vcs.dev/) — このリポは colocated 構成

## 初回セットアップ

```bash
git clone <url> patreon-archiver
cd patreon-archiver

# .env をローカルに作る (git-ignored)
cp .env.example .env
# WSL2 で host UID/GID を合わせる場合のみ:
sed -i "s/^UID=.*/UID=$(id -u)/; s/^GID=.*/GID=$(id -g)/" .env

# uv.lock を Docker 経由で生成 (host を汚さない)
just relock

# image build
just build

# 環境スモーク (ネット不要)
just smoke
```

`just smoke` が通れば環境は OK。

## 使い方

### 単発 download

```bash
just download "https://stream.example.com/<日付>_<id>_<token>/"
```

トークン付き URL なら Cookie 不要のはず。401/403 が返ったら下を参照。

### Cookie 経由

1. Firefox/Chrome の cookies.txt 拡張で配信ページの Cookie を Netscape 形式で export
2. `secrets/cookies.txt` に保存 (このディレクトリは git-ignored、`chmod 600` 推奨)
3. `just download-cookies "<URL>"`

### バッチ

`urls/urls.txt.example` を参考に `urls/urls.txt` を作る (git-ignored)。

```bash
just batch
```

`yt-dlp.conf` の `--download-archive` 設定により、すでに落とした URL は
自動的にスキップされる(`<DOWNLOAD_DIR>/archive.txt` が状態ファイル)。

### Patreon ページの inventory(俺的リスト化)

Patreon の "posts" ページをブラウザで全ポスト読み込んだ状態で MHTML
保存(Chrome / Edge: 右クリック → 「Web ページを単一ファイルで保存」)、
そのファイルを inventory にかけると 1 ポスト = 1 セクションの markdown
が出る。タイトル / 日付 / 動画長 / Patreon post URL の見出しの下に、
`# title: / # uploader: / # date: / # post:` のメタデータコメント付きの
**コピペ用 URL ブロック**(fenced code block)が並ぶ。欲しい動画の
ブロックの中身を丸ごと `urls/urls.txt` にコピーして `just batch`。

```bash
just inventory ~/Downloads/foo.mhtml > urls/posts.md
```

メタデータコメントは `download.sh` が読み取って `--parse-metadata` で
yt-dlp に注入される。Cloudflare Stream extractor は uploader/title/日付を
返さないので、これがないとファイル名が `NA/NA_<hex>_[<hex>].mp4` に
なる(参考: 該当 issue は `scripts/download.sh` の冒頭コメント)。

非動画 post(selfie / wallpaper / お知らせ)は stream URL なしで出るので
すぐ識別できる。

### 差分 sync(`just sync`)

新しい post だけを拾いたい場合:

```bash
just sync ~/Downloads/foo.mhtml
```

これは `inventory.py --seen-file urls/seen_posts.txt --minimal` の出力を
`urls/urls.txt` に書き、URL が 1 本でもあれば `just batch` を起動、終了後に
ハンドルした post の Patreon canonical URL を `urls/seen_posts.txt` に
append する(`sort -u` で dedup)。MHTML を週次で上書き保存 → `just sync`
を回すだけで新規 post が自動で降ってくる。

#### Gap detection(`urls/coverage.txt`)

MHTML は **完全である必要はない** — 初期表示の 10〜20 件だけで十分。`sync`
は `urls/coverage.txt` に **anchor 日**(= 直近の "good sync" 時の最新
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
「新規分のみ」運用が始められる(`coverage.txt` は次回 `just sync` 実行時に
自動 init される):

```bash
docker compose run --rm -T \
  -v "$(realpath ~/Downloads/foo.mhtml)":/in.mhtml:ro \
  --entrypoint python3 archiver \
  /work/scripts/inventory.py /in.mhtml --minimal \
  | grep '^# post: ' | sed 's/^# post: //' | sort -u \
  > urls/seen_posts.txt
```

`urls/` は丸ごと `.gitignore` 配下なので `seen_posts.txt` も `coverage.txt`
も git に乗らない。

## ダウンロード中の見え方

`docker compose run` が TTY を割り当てるので、yt-dlp の **進捗バーは
ターミナルにそのまま in-place で更新される**。`--progress-delta 5` で
更新頻度を 5 秒に絞ってあるので、TTY でも piped でも騒がしくならない。

```
[start] Example Creator — Sample post title (id=abc123...)
[download]  47.2% of   1.45GiB at  9.87MiB/s ETA 02:14 (frag 87/372)
[batch] sleeping 12s before next URL
[start] Example Creator — Sample bonus post (id=def456...)
```

- パーセンテージ / 残時間 / 速度 / fragment カウンタは 5 秒間隔で更新
- 動画の開始時 (`[start] ...`) と完了時 (`[done] ... -> /downloads/...`) を
  改行で残すので、ログをスクロールバックすると履歴が読める
- バッチ中の動画間 sleep は `[batch] sleeping Ns before next URL` で見える
  (`download.sh` 側で実装。`YTDLP_BATCH_SLEEP_MIN`/`MAX` で範囲調整)
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
| `YTDLP_BATCH_SLEEP_MIN` / `MAX` | `5` / `15` 秒 (batch のみ) | 動画間に `download.sh` が bash 側で random sleep(次のクリックまでの数秒) |
| `--retry-sleep extractor/http/fragment` | exp backoff 上限 30-60 秒 | 429 等を踏んだら自動で指数 backoff(本物のセーフティネット) |

50 本 × 1 時間 HD(1 本 ≈ 3.5GB)を 10 MB/s で引くと、1 本 6 分 + 動画間 ≈ 10 秒で
合計 5 時間強。一晩で終わるオーダー。

帯域を意図的に絞りたい(あるいは更に並列を上げたい)場合は `config/yt-dlp.conf`
を編集。バッチ間の sleep は `.env` の `YTDLP_BATCH_SLEEP_MIN` / `YTDLP_BATCH_SLEEP_MAX`。

ある一回だけ動作を変えたい場合は CLI 側で上書き可能:

```bash
just download "<URL>" --limit-rate 2M --concurrent-fragments 1
```

(yt-dlp は同じ flag が複数あれば後勝ち)

## 保存先を切り替える (外付け SSD)

`.env` の `DOWNLOAD_DIR` を書き換えるだけ。image rebuild は不要。

```bash
# 例: WSL2 にマウントした SSD
echo "DOWNLOAD_DIR=/mnt/wsl/ssd/patreon" >> .env
mkdir -p /mnt/wsl/ssd/patreon
just download "<URL>"
```

## yt-dlp の月次更新

1. `gh release list -R yt-dlp/yt-dlp -L 3` で最新版を確認
2. `pyproject.toml` の `yt-dlp[default]==<X.Y.Z>` を書き換え
3. `just relock`
4. `just upgrade` (`--no-cache --pull` で rebuild)
5. `just smoke` を確認 → `jj describe -m "chore(deps): bump yt-dlp to <X.Y.Z>"`

## ディレクトリ構成

```
patreon-archiver/
├── Dockerfile              # python:3.13-slim + ffmpeg + yt-dlp via uv
├── compose.yaml            # default / cookies / dev の 3 profile
├── compose.override.yaml   # scripts/ と config/ を bind mount
├── justfile                # `just <recipe>` で全操作
├── pyproject.toml          # yt-dlp の version pin
├── uv.lock                 # 再現性ロック
├── config/yt-dlp.conf      # 共通フラグ
├── scripts/
│   ├── download.sh         # entrypoint (Cookie の有無で分岐)
│   └── smoke.sh            # offline 検証
├── secrets/                # cookies.txt の置き場 (git-ignored)
├── downloads/              # default 保存先 (git-ignored)
└── urls/urls.txt           # バッチ実行用 URL 一覧 (git-ignored)
```

## 設計メモ

- 認証は presigned URL 前提で **Cookie なしから試す**。403/401 だけ Cookie 経路。
- ファイル名は **日本語そのまま** (`--restrict-filenames` は使わない)。
- HLS 想定で `--concurrent-fragments 4` と `--retry-sleep fragment:exp=1:60`。
- メタデータは inventory が emit するブロックから `download.sh` が
  `--parse-metadata` で注入。CloudflareStream extractor は uploader/title/
  date を一切返さないので、wrapper 側で埋めないとファイル名が `NA` 連発になる。
- mp4 の `comment` タグには JWT iframe URL ではなく Patreon post URL を埋め込む
  (token expire しても永続的に追跡できる canonical reference)。
- サイドカー(`*.info.json` / `*.description` / `*.jpg`)は出さない方針。
  サムネは `--embed-thumbnail` で mp4 内に取り込んで完結させる。
- DRM はかかっていない前提。Widevine 等が出てきた場合このツールは扱わない。

## ライセンス

MIT (`LICENSE`)。配信される動画コンテンツ自体は当然このライセンスの対象外で、
個人視聴目的に限定して利用する。
