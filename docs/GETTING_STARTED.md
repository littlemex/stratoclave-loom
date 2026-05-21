# stratoclave-loom: Getting Started Guide

**最終更新**: 2026-05-21
**対象者**: 初学者、新規参加メンバー

## はじめに

stratoclave-loom は **Pure Python のコーディングエージェント抽象化ライブラリ**
です。Claude Code / OpenCode / Kiro / Codex などの CLI を、ACP 風の統一
インターフェースで呼び出すための薄い層を提供します。

このガイドでは、ローカル開発環境を立ち上げて mock backend に対して 1 ターン
やり取りを送信できる状態までを案内します。

## 前提知識

- Python 3.11 以上 (3.12 推奨)
- 仮想環境の作成 (`venv`) と `pip install`
- 非同期 (`asyncio`) の基本

参考リンク:
- Python 公式ドキュメント: <https://docs.python.org/3.12/>
- ACP (Agent Client Protocol, Zed): 仕様 draft

## 環境セットアップ

### 1. リポジトリ取得

```bash
git clone https://github.com/littlemex/stratoclave-loom.git
cd stratoclave-loom
```

### 2. 仮想環境と依存関係

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

期待される最後の行:

```
Successfully installed stratoclave-loom-0.1.0.dev0 ...
```

### 3. 動作確認

```bash
stratoclave-loom list-backends
```

出力:

```
claude_code
mock
```

```bash
stratoclave-loom run --backend mock --message "hi"
```

出力 (1 行ずつの JSON):

```
{"session_id": "...", "chunk_type": "text_delta", "content": {"text": "h"}, "seq": 0}
{"session_id": "...", "chunk_type": "text_delta", "content": {"text": "i"}, "seq": 1}
{"session_id": "...", "chunk_type": "end_turn", "content": {}, "seq": 2}
```

## クイックスタート (Python API)

`examples/quickstart.py` 相当のコード:

```python
import asyncio
from stratoclave_loom import BackendConfig, create_session


async def main() -> None:
    cfg = BackendConfig(
        backend="mock",
        cwd=".",
        env={},
    )
    async with create_session(cfg) as session:
        stream = await session.send_message("hello")
        async for chunk in stream:
            print(chunk.chunk_type, chunk.content)
            if chunk.chunk_type == "end_turn":
                break


asyncio.run(main())
```

## 各システムの使い方

### コア型

| 型 | 役割 |
|---|---|
| `BackendConfig` | バックエンド名、cwd、環境変数、resume パスなど起動設定 |
| `AgentSession` | 1 つの agent サブプロセスを抽象化 (`async with` 必須) |
| `AcpChunk` | 正規化された出力単位 (`text_delta` / `tool_use` / `end_turn` / ...) |
| `NormalizedTurn` | JSONL の 1 行を正規化した turn (上位層が DB 化する) |
| `PermissionRequest` | tool 実行許可要求 (UI が応答を返す) |

### バックエンド

- `mock`: テスト・デモ用。subprocess を起動せず、入力を 1 文字ずつ
  `text_delta` として返す。
- `claude_code`: Claude Code CLI 用 adapter (v0.1 ではスケルトン、
  `normalize` のみ動作する)。

新しい adapter を追加する場合は `src/stratoclave_loom/adapters/` 配下に
モジュールを追加し、`AgentBackend` を継承して `register_backend` で名前を
登録します。

### 環境変数

| 変数 | 既定 | 用途 |
|---|---|---|
| `STRATOCLAVE_LOOM_LOG_LEVEL` | `INFO` | 内部ログレベル |
| `STRATOCLAVE_LOOM_BUFFER` | `65536` | stdio バッファ上限 (byte) |
| `STRATOCLAVE_LOOM_CANCEL_GRACE_MS` | `500` | SIGINT→SIGTERM の猶予 (ms) |

`BackendConfig.env` で渡した値は agent サブプロセスに **そのまま伝搬**
されます。例: `ANTHROPIC_BASE_URL` を指定すると Claude Code がそれを使う。

## テストの実行

```bash
pytest
```

期待される出力:

```
============================== 18 passed in 0.X s ==============================
```

カバレッジ付き:

```bash
pytest --cov=stratoclave_loom --cov-report=term-missing
```

E2E テスト (実 agent CLI が必要、既定では skip):

```bash
pytest -m e2e
```

## トラブルシューティング

### `ImportError: stratoclave_loom`

仮想環境が有効か確認:

```bash
which python   # .venv 内を指していること
pip list | grep stratoclave-loom
```

### `BackendNotFoundError`

`list_backends()` で登録済みかを確認。adapter モジュールが import されて
いないと登録されません (`src/stratoclave_loom/adapters/__init__.py` で
import が走ります)。

### CLI で `claude_code` が `NotImplementedError`

v0.1 ではスケルトン状態です。`normalize` のテストには使えますが
`send_message` は未実装です。

## 次のステップ

- [PROJECT_STATUS.md](./PROJECT_STATUS.md): 現在の実装状況
- [PROJECT_RULES.md](./PROJECT_RULES.md): プロジェクト固有ルール
- [DESIGN.md](./DESIGN.md): シリーズ全体の設計判断
