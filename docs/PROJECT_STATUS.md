# stratoclave-loom 実装状況

**最終更新**: 2026-05-21
**プロジェクト開始**: 2026-05-21

## 総合進捗

### 実装完了状況 (v0.1 Walking Skeleton)

| モジュール | 状態 | テスト | 備考 |
|---|---|---|---|
| `core.types` (`AcpChunk` / `BackendConfig` / `NormalizedTurn` / `PermissionRequest`) | 完了 | 間接的 | dataclass、frozen |
| `core.errors` (`LoomError` 階層) | 完了 | 間接的 | 5 種類 |
| `core.backend` (`AgentBackend` ABC) | 完了 | 間接的 | 7 メソッド |
| `core.session` (`AgentSession`) | 完了 | 完了 | async context manager |
| `config` (env loader, `LoomSettings`) | 完了 | 完了 | デフォルト + override |
| `transport.stdio` (`StdioTransport`) | 完了 | 完了 | spawn / send / receive / cancel |
| `runtime.process_pool` (`ProcessPool`) | 完了 | 完了 | semaphore lease |
| `adapters._registry` (`register_backend` / `get_backend`) | 完了 | 完了 | グローバル dict |
| `adapters.mock` (`MockBackend`) | 完了 | 完了 | text_delta + end_turn + cancel |
| `adapters.claude_code` (`ClaudeCodeBackend`) | スケルトン | 部分完了 | `normalize` のみ動作、send_message は未実装 |
| `cli` (`stratoclave-loom run/list-backends`) | 完了 | 完了 | argparse、JSONL 出力 |

### 品質チェック

| 項目 | 結果 |
|---|---|
| pytest (18 件) | PASS |
| ruff lint | PASS |
| ruff format | PASS |
| mypy strict | PASS |
| Python | 3.12 で動作確認、CI で 3.11/3.12 を実行予定 |

### 統合状況

| 連携先 | 状態 | 備考 |
|---|---|---|
| stratoclave (Bedrock proxy) | 未連携 | env passthrough のみ。直接依存なし |
| stratoclave-distill | 未着手 | 別 OSS、まだ未開発 |
| stratoclave-atelier | 未着手 | 別 OSS、まだ未開発 |
| claude-capture (移行元) | 未着手 | atelier 完成後に置き換える計画 |

## 完了した作業

### 2026-05-21

- DESIGN.md (4 OSS 全体設計、1014 行) を確定
- リポジトリ骨組み (LICENSE / README / CONTRIBUTING / SECURITY / CODE_OF_CONDUCT / .gitignore)
- pyproject.toml (Python 3.11+ / Apache 2.0 / pytest+ruff+mypy+coverage)
- core / config / transport / adapters / runtime / cli モジュール
- mock backend と claude_code backend (skeleton)
- 単体テスト 18 件、CLI smoke、StdioTransport ラウンドトリップ
- GitHub Actions CI ワークフロー
- 必須ドキュメント 3 点 (本ファイル含む)

## 技術的な成果

- **Pure Python**: 標準ライブラリのみで動作 (実行時依存ゼロ)
- **mypy strict**: 型エラーゼロ
- **AgentBackend ABC**: 7 メソッドで CLI 抽象化を表現
- **stdio JSON-RPC framing**: subprocess の lifecycle を `start/send/receive/cancel/close` で制御
- **環境変数 passthrough**: stratoclave 連携は env のみ (直接依存なし)

## 未実装 / 将来対応

### v0.1 残タスク (優先度順)

| 優先度 | タスク |
|---|---|
| 高 | `ClaudeCodeBackend.send_message` の wire-level 実装 (Claude Code CLI の headless モードで stdio 経由) |
| 高 | `ClaudeCodeBackend.cancel` / `close` / `handle_permission` の実装 |
| 中 | `examples/quickstart.py` の追加 |
| 中 | `pre-commit` 設定 |
| 中 | E2E テスト (実 Claude Code を使う、`pytest -m e2e`) |
| 低 | `StdioTransport.receive` の back-pressure テスト |

### v0.2

- OpenCode adapter (PoC 後) と native ACP 接続
- `ProcessPool` の優先度 / queue 詳細制御
- `pyproject.toml` の `Programming Language :: Python :: 3.13` 追加

### v0.3

- Kiro adapter (PoC 後)
- Codex adapter (PoC 後)
- `allowed_tools` の adapter 側強制
- `resume_from_jsonl` を全 adapter で対応

### v1.0

- Public API 凍結 (semver 1.x)
- 性能改善 (subprocess pool reuse、stdio の zero-copy)

## チーム体制

| 役割 | Agent | 状態 | 現在のタスク |
|---|---|---|---|
| Owner | littlemex | active | DESIGN / PR レビュー |
| 実装担当 | Claude Code | active | v0.1 wire-level (ClaudeCodeBackend.send_message) |

## 次のステップ

優先度順:

1. **`ClaudeCodeBackend.send_message` 実装** — Claude Code CLI を spawn し、stdio JSONL を `AcpChunk` に変換する
2. **E2E テストハーネス** — 実 Claude Code をオプションで spawn して 1 turn 動作確認
3. **`examples/quickstart.py`** — README / GETTING_STARTED と整合する最小サンプル
4. **stratoclave-distill のリポジトリ作成** — 設計に従い別 OSS としてスタート (loom 実装と並行)

## リンク

- [GETTING_STARTED.md](./GETTING_STARTED.md)
- [PROJECT_RULES.md](./PROJECT_RULES.md)
- [DESIGN.md](./DESIGN.md)
- [README.md](../README.md)
