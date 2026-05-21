# stratoclave-loom プロジェクトルール

**最終更新**: 2026-05-21

このドキュメントは stratoclave-loom 固有の開発ルールを集約します。一般的な
コントリビュータ向けの手順は [CONTRIBUTING.md](../CONTRIBUTING.md) を参照
してください。

## 設計原則

### 1. Pure Python を維持する

- ランタイム依存は **追加しない** (標準ライブラリのみ)。
- どうしても必要な場合は optional extras (`pip install stratoclave-loom[xxx]`)
  にし、import 時に lazy import する。
- C 拡張、Docker、外部バイナリへの直接依存も同様に避ける。

### 2. ハードコード絶対禁止

- パス、URL、モデル名、トークン、ポート番号、CLI 引数を**直書きしない**。
- 設定は (a) `BackendConfig` / 関数引数、(b) 環境変数、(c) 設定ファイル
  のいずれかから流す。
- 例外: テストフィクスチャ内の固定値、定数化された tool 名マップ
  (`_NORMALIZED_FROM_NATIVE`)。

### 3. 単一責任 / 上位層に依存しない

- loom は **1 つの agent サブプロセスを抽象化する** ことだけを責務とする。
- セッション DB、グループ、バージョン管理、検索、UI はすべて上位層
  (atelier / distill) の責務。loom にこれらの機能を入れない。
- 上位層を import しないこと (循環依存禁止)。

### 4. ACP 中心、agent 固有の漏れは adapter に閉じる

- 公開 API は ACP 風 (initialize / send_message / cancel / handle_permission)
  に揃える。
- agent CLI 固有の API、フラグ、JSONL 形式は `adapters/<name>.py` の中だけで扱う。
- ツール名は **正規化名** (`shell.run` / `file.write` ...) で公開層に出す。
  agent 固有の名前 (`Bash` 等) は adapter 内部のマップで変換する。

## コーディング規約

### 命名規則

- パッケージ: `stratoclave_loom`
- 公開 API: `snake_case` 関数、`PascalCase` クラス、頭文字大文字の頭字語
  禁止 (例: `AcpChunk` ではなく `ACPChunk` は不可)
- private: 先頭 `_` (例: `_NORMALIZED_FROM_NATIVE`)
- ABC: `class Foo(ABC):` で `@abstractmethod`

### ファイル構成

```
src/stratoclave_loom/
├── __init__.py            # 公開 API のみ re-export
├── config.py              # env 経由の設定
├── cli.py                 # `stratoclave-loom` コマンド
├── core/                  # 型 / プロトコル / 例外 / セッション
├── transport/             # subprocess 通信 (現状: stdio のみ)
├── adapters/              # backend 実装 (1 ファイル = 1 backend)
└── runtime/               # process pool 等のヘルパー
```

各 module は **小さく、責務単一**。1 ファイル 500 行を超えそうなら分割を検討する。

### docstring と型

- すべての公開関数 / クラスに docstring を付ける (1 行サマリ + 必要なら詳細)。
- `mypy --strict` でグリーンを維持する。`Any` を返す関数は再考すること。
- `from __future__ import annotations` を全モジュールで有効化。

### テスト方針

- **単体テスト** (`tests/unit/`): 純粋ロジック、I/O なし
- **adapter テスト** (`tests/adapters/`): mock backend 等、実 CLI 不要
- **E2E テスト** (`tests/e2e/`): 実 agent CLI 必要、既定では skip
  (`pytest -m e2e` で実行)
- カバレッジは 80% 以上を目標。新規 PR で大きく下げない。
- async テストは `pytest-asyncio` の `auto` モードで `async def`
  テスト関数のまま書ける。

## Git ワークフロー

### ブランチ戦略

- `main`: リリース可能 (CI green) を維持。
- `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>` などの
  feature branch を main から切る。
- alpha 期 (v0.1) は破壊的変更 OK だが、PR 説明で明記する。

### コミットメッセージ

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>
```

scope 例: `core`, `adapters`, `transport`, `cli`, `tests`, `docs`, `ci`。

### マージ手順

1. PR を出す前にローカルで `ruff check . && ruff format --check . && mypy src/stratoclave_loom && pytest`
2. CI green を確認
3. レビュアー 1 名以上の approve
4. squash merge (履歴を平坦化)

## コミュニケーションルール

- レビューは GitHub PR コメントで行う。日本語 / 英語どちらでも可。
- セキュリティ問題は GitHub Private Vulnerability Report を使う
  (`SECURITY.md`)。public issue / PR には書かない。
- 大きな設計変更は事前に issue を立てて議論する。

## プロジェクト特有の制約

### 1. Soft Guard 方針 (sandbox)

- 過去 agent 起動時の副作用抑止は **本ライブラリの責務外**。
  上位層 (atelier) が `chmod 0444` + `allowed_tools` で制限する。
- loom 自身が OS sandbox / Docker を扱うことはしない。

### 2. cross-agent replay は対象外

- ある agent の JSONL を別 agent に食わせて **完全再生** する機能は持たない。
- `normalize` で取り出した text をコンテキスト参照する用途のみ想定。

### 3. backend 名は不変識別子

- 一度公開した `backend_name` (例: `claude_code`) は **renames しない**。
  互換性のために古い名前を alias として残す。

### 4. stratoclave への直接依存禁止

- loom は stratoclave (Bedrock proxy) を **直接 import / HTTP しない**。
- `BackendConfig.env` で渡された URL / トークンを subprocess に
  パススルーするだけ。

### 5. Python バージョン

- 最低: Python 3.11 (CI で検証)。
- 新機能は 3.11 で動くこと。3.12 以降の新構文を使う場合は明示的に
  `requires-python` を上げる PR を別途出す。

## チェックリスト (PR 提出前)

- [ ] `ruff check .` 通過
- [ ] `ruff format --check .` 通過
- [ ] `mypy src/stratoclave_loom` 通過
- [ ] `pytest` 通過 (E2E は別途 `-m e2e` で確認)
- [ ] 公開 API 変更なら `docs/PROJECT_STATUS.md` 更新
- [ ] 設計判断を変えたなら `docs/DESIGN.md` を更新 or ADR 追加
- [ ] ハードコードしていないか確認 (パス、URL、モデル名)
- [ ] 新 dependencies の追加は本 PR の必要性を説明
