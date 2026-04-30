# Discord 時報 Bot

ずんだもんの声で「X時になったのだ」「X時半なのだ」「X時Y分なのだ」をボイスチャンネルに流す Discord Bot。
音声合成は **ローカル** で VOICEVOX エンジンを使い `voices/*.wav` に焼き込み、
サーバー側はその wav を再生するだけ。エンジンを本番に同梱しない構成。

## 仕組み

- `/jiho` — トグルコマンド。未接続なら呼び出した人の VC に参加 (参加直後に `connected.wav`「時報を開始するのだ」)、接続済みなら切断
- `/setting` — 時報の間隔をドロップダウンから選択 (毎時0分・30分(既定) / 毎時0分のみ / 10分ごと)。VC 接続中なら確認音 `interval_<N>.wav`「N分ごとに変更したのだ」を再生。設定は guild ごとに保持され、切断/再接続では保たれる(再起動で消える)
- 内部スケジューラがタイムゾーン (`JIHO_TIMEZONE`、既定 `Asia/Tokyo`) の境界に起き、各 guild の設定に応じて `voices/<hour>[_<minute>].wav` を再生
- 状態は in-memory のみ。再起動で VC 接続も interval 設定も失われる

## セットアップ

### 1. 音声を生成 (ローカル)

> **注意**: Bot 用 Docker イメージ ([Dockerfile](Dockerfile)) は `voices/` を `COPY` するので、**最初にこの手順を済ませる必要がある**。空のまま `docker compose up` すると Bot は起動するが時報が無音になる。

`docker compose` 1 コマンドで VOICEVOX エンジンの起動 → wav 生成 → 全停止までやる:

```bash
docker compose -f docker-compose.gen.yml up --build \
    --abort-on-container-exit --exit-code-from gen
```

`voices/` 以下に **148 ファイル** 一括で生成される(48kHz / stereo / 16bit。
ffmpeg を経由せず `discord.PCMAudio` でそのまま流せる形式)。

| パターン | ファイル | 既定読み |
|---|---|---|
| `<hour>.wav` | 24 個 (0〜23時) | 「午前/午後X時になったのだ」 |
| `<hour>_30.wav` | 24 個 | 「午前/午後X時半なのだ」 |
| `<hour>_10/20/40/50.wav` | 96 個 | 「午前/午後X時Y分なのだ」 |
| `connected.wav` | 1 個 | 「時報を開始するのだ」 (`/jiho` で接続時) |
| `interval_60/30/10.wav` | 3 個 | 「N分(or1時間)ごとに変更したのだ」 (`/setting` 変更時) |

既存の wav はスキップされる。再生成したいときは:

```bash
GEN_ARGS="--force" docker compose -f docker-compose.gen.yml up --build \
    --abort-on-container-exit --exit-code-from gen
```

`GEN_ARGS` には `--speaker 1`、`--template ...`、`--template-half ...`、`--template-minute ...` 等を任意に渡せる。

Python で直接走らせたい場合(VOICEVOX を別途起動済みの環境):

```bash
docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest
pip install -e '.[gen]'
python scripts/generate_voices.py
```

オプション (全て CLI フラグで上書き可能、`GEN_ARGS` 経由で compose にも渡せる):

| 引数 | 既定 | 説明 |
|---|---|---|
| `--engine` | `http://localhost:50021` | VOICEVOX エンジンの URL |
| `--speaker` | `3` | スピーカー ID(3 = ずんだもん ノーマル) |
| `--template` | `{period}{hour12}時になったのだ` | :00 用テンプレ。`{period}`=午前/午後、`{hour12}`=0..11、`{hour}`=0..23 |
| `--template-half` | `{period}{hour12}時半なのだ` | :30 用テンプレ |
| `--template-minute` | `{period}{hour12}時{minute}分なのだ` | :10/:20/:40/:50 用。`{minute}` も使える |
| `--text-connected` | `時報を開始するのだ` | `/jiho` 接続時の `connected.wav` (テンプレ変数なし、そのまま読み上げ) |
| `--text-interval-60` | `1時間ごとに変更したのだ` | `/setting` で 60 分にしたときの `interval_60.wav` |
| `--text-interval-30` | `30分ごとに変更したのだ` | `/setting` で 30 分にしたときの `interval_30.wav` |
| `--text-interval-10` | `10分ごとに変更したのだ` | `/setting` で 10 分にしたときの `interval_10.wav` |
| `--out-dir` | `voices/` | 出力ディレクトリ |
| `--force` | off | 既存の wav を上書き |
| `--wait-seconds` | `15` | エンジン起動待ちの最大秒数 (compose では 90 を渡している) |

### 2. Discord Bot を用意

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** → **Bot** タブで **Reset Token**
2. **OAuth2 → URL Generator** で招待 URL を生成:
   - **Scopes**: `bot`, `applications.commands`
   - **Bot Permissions**: `Connect`, `Speak`, `Send Messages`
3. Privileged Gateway Intents は不要

### 3. 起動

```bash
cp .env.example .env  # DISCORD_TOKEN を埋める
docker compose up --build
```

開発中は `.env` に `DISCORD_GUILD_IDS=<サーバ ID>` を入れるとコマンドが即時同期される。

### 4. Railway

1. GitHub リポジトリを連携、Root Directory はそのまま
2. 環境変数 `DISCORD_TOKEN` を設定
3. Push すると [railway.toml](railway.toml) → [Dockerfile](Dockerfile) で起動

## 環境変数

| 変数 | 既定 | 説明 |
|---|---|---|
| `DISCORD_TOKEN` / `DISCORD_TOKENS` | — | 単一トークン または CSV で複数 Bot を 1 プロセス並走 (両方指定可、重複は除外) |
| `DISCORD_GUILD_IDS` | 空 | カンマ区切りの guild ID。指定すると即時コマンド同期 |
| `JIHO_TIMEZONE` | `Asia/Tokyo` | 時報を打つタイムゾーン (IANA 名) |
| `LOG_LEVEL` | `INFO` | logging レベル |

## ディレクトリ

```
src/
├── main.py          # N 個の Bot を gather + signals
├── bot.py           # JihoBot + /jiho /setting ハンドラ + Select View
├── config.py        # pydantic-settings
├── constants.py     # paths / defaults
├── scheduler.py     # 多 cadence (60/30/10) の単一ループ broadcast
└── voice_manager.py # ギルド単位の VC 接続 / play / disconnect / interval
scripts/
└── generate_voices.py # ローカル専用: VOICEVOX → voices/*.wav 一括生成
voices/                # 148 wavs (時報144 + connected/interval_*) — Docker イメージへ COPY
tests/
```

## 開発

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,gen]'

ruff format --check .
ruff check src tests scripts
pytest -q
```

CI は push / PR で同じ 3 ステップを実行する ([.github/workflows/ci.yml](.github/workflows/ci.yml))。

## クレジット

- [VOICEVOX](https://voicevox.hiroshiba.jp/) / [VOICEVOX:ずんだもん](https://zunko.jp/)
- 構成は [pomodoro-bot](../pomodoro-bot) と [voicevox-discord](../voicevox-discord) を参考
