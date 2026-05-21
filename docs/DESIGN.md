# Stratoclave Series — 全体設計書

**最終更新**: 2026-05-21
**ステータス**: ドラフト (loom 実装着手前のベースライン)
**対象**: stratoclave-loom / stratoclave-distill / stratoclave-atelier の設計判断と境界の固定

このドキュメントは 4 OSS (stratoclave / stratoclave-loom / stratoclave-distill /
stratoclave-atelier) と claude-capture からの移行を含めた全体設計を 1 ファイルに集約した
ものです。loom リポジトリに置きますが、内容はシリーズ全体の設計判断記録です。

---

## 1. 背景

ユーザーは現在 `claude-capture` (Python ~5000 行、Claude Code 依存、static HTML UI)
を運用しているが、以下の課題がある。

- Claude Code 依存で他エージェント (OpenCode / Kiro / Codex) が使えない
- コードが乱雑で機能拡張が辛い
- セッション管理 / バージョン管理 / 過去状態への問い合わせができない
- **compact で会話が失われる**
- **セッションに混入したゴミがその後の対話を汚染する**
- **どのセッションが何を担っていたか後から不明**

これを 4 つの OSS と 1 UI に分解し、責務を分離して再構築する。

---

## 2. シリーズ構成と責務

```
stratoclave           : 認可・クレジット・監査 (Bedrock proxy, 既存)
stratoclave-loom      : 単一 agent 実行の抽象化 (新規, Pure Python lib)
stratoclave-distill   : session 蒸留 + 学習集約 + 検索 (新規, Pure Python lib + CLI)
stratoclave-atelier   : raw session DB + group / version 管理 + UI (新規, Docker)
```

**4 つの命名意味階層**: 鍵 (clave) → 織機 (loom) → 蒸留 (distill) → 工房 (atelier)。
loom が織り、distill が蒸留し、atelier が組み合わせて作品を編む構図。

### 2.1 責務マトリクス

| 機能 | stratoclave | loom | distill | atelier |
|---|:-:|:-:|:-:|:-:|
| 認証 (Cognito/SSO/API key) | O | - | - | - |
| クレジット quota | O | - | - | - |
| Anthropic API proxy | O | - | - | - |
| API レベル監査ログ | O | - | - | - |
| ACP プロトコル抽象化 | - | O | - | - |
| agent adapter (CC/OC/Kiro/Codex) | - | O | - | - |
| 単一 session の送受信 / cancel / streaming | - | O | - | - |
| stdio transport / process spawn | - | O | - | - |
| `resume_args()` 提供 (frozen jsonl 引き渡し) | - | O | - | - |
| jsonl 正規化 (`NormalizedTurn`) | - | O | - | - |
| **session 蒸留 (purpose / digest / learnings)** | - | - | **O** | - |
| **watermark 管理 (claude-smart 方式)** | - | - | **O** | - |
| **dedup / merge / supersede / archive** | - | - | **O** | - |
| **group / cross-session rollup** | - | - | **O** | - |
| **embedding (vector index)** | - | - | **O** | - |
| **BM25 / 全文検索** | - | - | **O** | - |
| **context pack export (token budget)** | - | - | **O** | - |
| **MCP server (将来 v0.x)** | - | - | **O** | - |
| **pollution mark の伝搬と除外** | - | - | **O** | - |
| グループ化 / タグ | - | - | - | O |
| JSONL バージョン管理 (DAG, freeze) | - | - | - | O |
| raw jsonl blob 保管 | - | - | - | O |
| Snapshot 起動 orchestration | - | - | - | O |
| メイン↔過去 agent RPC | - | - | - | O |
| Cross-Session 通信ハブ | - | - | - | O |
| Web UI / REST / WebSocket / SSE | - | - | - | O |
| atelier 自身の認証 (optional) | - | - | - | O |
| 環境変数注入 (`ANTHROPIC_BASE_URL` 等) | - | passthrough | - | 注入元 |

### 2.2 重要原則

- **loom は stratoclave も distill も知らない**。単一 session の実行に閉じる。
- **distill は loom も atelier も知らない**。jsonl と session メタを受け取って蒸留・検索を返す純関数的ライブラリ。
- **atelier は loom と distill を組み合わせる orchestrator**。stratoclave とは env 経由のみで連携。
- 各 OSS は単独動作可能。loom / distill は Pure Python lib (Docker 不要)、atelier のみ Docker 必須。
- **distill は単独で価値**: 任意 jsonl を食わせて学びを蓄積する DB として、atelier 抜きでも使える。MCP / HTTP server で他 IDE / agent からも利用可能 (MCP は v0.x 後半)。

---

## 3. 全体アーキテクチャ図

```
                       +-------------------------+
                       |  Browser (vanilla JS)   |
                       +------------+------------+
                                    | HTTP / WebSocket / SSE
                                    v
+----------------------------------------------------------------------+
|  stratoclave-atelier  (Docker on finch / docker compose)              |
|  --------------------------------------------------------------------|
|  ・FastAPI (REST + WebSocket + SSE)                                  |
|  ・Postgres (SQLAlchemy 2.x async, pgvector 拡張)                    |
|  ・Version Store (content-addressed blob, host volume mount)         |
|  ・Group Manager / SnapshotRuntime / Cross-Session RPC Hub           |
|  ・UI (claude-capture から移植した static HTML)                      |
|  ・Auth (optional, Bearer / stratoclave Cognito 委譲)                |
+----+------------------------------------+---------------------------+
     | in-process import                  | in-process import
     v                                    v
+----------------------------+   +-----------------------------------+
| stratoclave-loom           |   | stratoclave-distill                |
| (Pure Python lib)          |   | (Pure Python lib + CLI [+ MCP/HTTP|
|                            |   |  server, optional])                |
| ・AgentBackend Protocol    |   | ・Distiller (LLM 抽出)             |
| ・Adapters (CC/OC/Kiro/    |   | ・Curator (dedup/merge/supersede) |
|   Codex)                   |   | ・Aggregator (group rollup)        |
| ・StdioTransport           |   | ・Retriever (vector + BM25)        |
| ・ProcessPool              |   | ・ContextPacker (token budget)     |
| ・ACP 正規化               |   | ・Watermark Manager                |
+----+-----------------------+   +-----+-----------------------------+
     | subprocess (stdio)               | jsonl ingest (push) /
     | env passthrough                  | search query (pull)
     v                                  v
+--------------------------------+   +-------------------------------+
| Agent CLI process              |   | Postgres + pgvector            |
| (claude / opencode / kiro /    |   | (atelier と同 DB を相乗り or   |
|  codex)                        |   |  別 DATABASE_URL で別建ても可)|
+----+---------------------------+   +-------------------------------+
     | HTTPS (Anthropic Messages API)
     v
+--------------------------------+
| stratoclave  (既存, 認可)      |
| ・Cognito / SSO / API Key      |
| ・credit quota / audit log     |
| ・/v1/messages -> Bedrock      |
+----+---------------------------+
     | AWS IAM
     v
  Amazon Bedrock
```

**データの動き**:
- agent 実行: atelier -> loom -> agent CLI -> stratoclave -> Bedrock (synchronous turn)
- raw jsonl 保管: atelier の Version Store (content-addressed blob)
- 蒸留パイプライン: atelier -> distill (raw jsonl と session メタを ingest) -> Postgres へ digest/learnings 永続化
- 検索 / inject: atelier (or 任意 client) -> distill (query) -> ContextPack を返却 -> agent 起動時に inject

---

## 4. データフロー

### 4.1 通常のセッション送信

```
1. User -> Browser -> POST /api/sessions/{id}/messages  (atelier)
2. atelier: session, group, version_id を解決
   2-a. distill.retrieve(query=user_input, group_id, agent_type, budget_tokens=N)
        -> 関連 learnings + 関連 session_digest を ContextPack として取得
   2-b. ContextPack を agent への initial system context として注入
3. loom.send_message() を呼ぶ
4. loom: AgentAdapter で claude/opencode/... の subprocess を起動
        env: ANTHROPIC_BASE_URL=<atelier 設定の stratoclave URL>
             ANTHROPIC_AUTH_TOKEN=<同 user の sk-stratoclave-...>
5. agent process: stratoclave に POST /v1/messages
6. stratoclave: 認証 -> quota -> Bedrock -> 監査ログ
7. agent process: stdout に ACP chunks
8. loom: chunks を NormalizedTurn に正規化し AsyncIterator で atelier に返す
9. atelier: chunks を JSONL に append、WebSocket でブラウザにストリーミング
10. atelier: turn 完了時に events テーブルへ記録、version 更新
11. atelier: distill.ingest_incremental(session_id, jsonl_path, watermark) を非同期で発火
            (turn 数閾値超過時のみ、または明示的な /learn 操作時)
```

### 4.2 過去 agent への問い合わせ (Snapshot Query)

```
1. メイン agent が tool call: query_archived_session(group_id, version_id, prompt)
   または UI で「v3 にピンして問い合わせ」をクリック
2. atelier.SnapshotRuntime:
     a. version_id の frozen blob を /tmp/<uuid>/session.jsonl に chmod 0444 でコピー
     b. AgentAdapter.resume_args(blob_path) でコマンドライン取得
     c. loom.spawn_snapshot_session()
        (allowed_tools=["Read","Grep"] を強制、env は通常と同じ)
     d. prompt を inject、応答を ACP chunks で受信
     e. tool_result としてメイン session の jsonl に記録
        raw_json には queried_session/queried_version/snapshot_runtime_id を保持
3. snapshot agent process: 終了 -> /tmp/<uuid> 削除
4. UI には snapshot 由来であることがバッジで表示される
```

メイン↔過去 agent 通信は **atelier が両者を仲介**するモデル (Orchestrator 仲介案)。
loom はあくまで「与えられた jsonl で agent を 1 つ起動するライブラリ」に留め、
複数 agent を協調させる責務は atelier が持つ。

### 4.3 蒸留パイプライン (distill)

distill は 3 段の独立コンポーネントを持ち、それぞれ非同期 / バッチで動作する。

```
[Stage 1] Extractor
  入力: session jsonl, watermark "published_up_to: N"
  処理: N 以降の turn を読み LLM に投げ、purpose / digest / learnings を抽出
  出力: session_purposes, session_digests, learnings (Postgres へ書き込み)
  発火: (a) turn 数が閾値を超えた時 (b) session freeze 時 (c) 手動 /learn 時

[Stage 2] Curator
  入力: 新規 learning と既存 learnings の vector / BM25 上位 K
  処理: cosine 類似度 >= τ_dup なら merge + evidence_count++
        意味的に矛盾するなら新が old を supersede (archived_at セット)
        evidence が一定期間更新されない project skill は shared への昇格を検討
  出力: 統合済み learnings

[Stage 3] Aggregator (周期実行)
  入力: 同一 group の learnings 集合
  処理: group rollup (LLM で 5-10 行に要約) + cross-session pattern 抽出
  出力: group_learnings (project skill より上位の集約)
```

**watermark**: 各 session に `(session_id, watermark) -> published_up_to` を持ち、
重複抽出を防ぐ。watermark より新しい turn のみ Stage 1 に渡す。

### 4.4 検索 / inject フロー

```
1. atelier (or 任意 client): distill.retrieve(query, filters, budget)
2. distill.Retriever:
     a. query を embedding (cloud or local) に変換
     b. pgvector で ANN 検索 (HNSW index, 上位 K1)
     c. tsvector で BM25 検索 (上位 K2)
     d. RRF (Reciprocal Rank Fusion) で hybrid マージ -> 上位 K3
     e. filter (group_id, agent_type, polluted=false, archived_at IS NULL) で除外
3. distill.ContextPacker:
     a. learnings の rule + why を優先
     b. 残り budget で session_digests.summary_md を追加
     c. token 数を tiktoken (or Anthropic tokenizer) で正確にカウント
     d. ContextPack (Markdown 形式 or 構造化 JSON) を返却
4. atelier: ContextPack を agent の system prompt or initial user message に注入
```

---

## 5. stratoclave-loom (中層ライブラリ) 詳細

### 5.1 コア API

```python
# stratoclave_loom/__init__.py
from stratoclave_loom import (
    AgentBackend,        # ABC
    AgentSession,        # 1 つの agent process を抽象化
    AcpChunk,            # 正規化された出力単位
    PermissionRequest,
    BackendConfig,       # env, cwd, allowed_tools 等
    NormalizedTurn,      # JSONL 正規化結果
    list_backends,       # 利用可能 backend 列挙
    create_session,      # AgentSession を作るファクトリ
)
```

### 5.2 最小利用例

```python
from stratoclave_loom import create_session, BackendConfig

cfg = BackendConfig(
    backend="claude_code",
    cwd="/Users/akazawt/myproject",
    env={
        "ANTHROPIC_BASE_URL": "https://stratoclave.example.com/v1",
        "ANTHROPIC_AUTH_TOKEN": "sk-stratoclave-xxx",
    },
    allowed_tools=None,        # None=全許可、list で制限
    resume_from_jsonl=None,    # None=新規、path=過去 jsonl から resume
)

async with create_session(cfg) as session:
    async for chunk in session.send_message("hello"):
        print(chunk)
```

### 5.3 主要 ABC / Protocol シグネチャ

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator
from dataclasses import dataclass

@dataclass(frozen=True)
class AcpChunk:
    session_id: str
    chunk_type: str          # "text_delta" | "tool_use" | "tool_result" | "end_turn"
    content: dict
    agent_id: str | None = None

@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str           # 正規化済み: "shell.run" | "file.write" | ...
    arguments: dict
    session_id: str

@dataclass(frozen=True)
class NormalizedTurn:
    turn_id: str
    session_id: str
    seq: int
    role: str                # "user" | "assistant" | "tool_use" | "tool_result"
    text_content: str
    tool_name: str | None
    tool_input: dict | None
    occurred_at: str
    raw_line: str            # 元の JSON 行をそのまま保持 (再正規化用)

class AgentBackend(ABC):
    """各エージェントバックエンドへの橋渡し。"""

    backend_name: str        # "claude_code" | "opencode" | "kiro" | "codex"

    @abstractmethod
    async def initialize(self, capabilities: dict) -> dict: ...

    @abstractmethod
    async def send_message(
        self,
        session_id: str,
        content: str,
        context_files: list[str] | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[AcpChunk]: ...

    @abstractmethod
    async def cancel(self, session_id: str) -> None: ...

    @abstractmethod
    async def handle_permission(
        self, request: PermissionRequest, granted: bool
    ) -> None: ...

    @abstractmethod
    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]: ...

    @abstractmethod
    def resume_args(self, frozen_jsonl_path: str) -> list[str]:
        """frozen_jsonl_path をコンテキストとして食わせるためのコマンドライン引数を返す。
        実装が resume をサポートしない場合は空リストを返す。"""
        ...
```

### 5.4 ACP プロトコル拡張 (loom 内部)

ACP 標準にないメソッドを `x-acp/` namespace で定義する。これらは loom と
atelier の間で使う内部 RPC で、agent に直接渡さない。

```
x-acp/session.snapshot         (atelier が発行) frozen blob を adapter に渡し起動
x-acp/session.queryArchived    (atelier が発行) snapshot agent に質問
x-acp/permission.respond       (atelier 経由 UI 応答) requestPermission への回答
```

### 5.5 各 agent の対応戦略 (実装前 PoC で要検証)

| agent | ACP 対応 | adapter 戦略 | resume |
|---|---|---|---|
| Claude Code | なし | SDK + jsonl 監視の hybrid shim | `--resume <session_id>` |
| OpenCode | あり (要検証) | ネイティブ ACP 接続 | 要検証 |
| Kiro | 不明 | プロセス起動 + stdout 監視 + jsonl tail | 要検証 |
| Codex | 不明 | OpenAI streaming format -> 共通 schema 変換 | 要検証 |

**v0.1 は Claude Code only**。他 backend は v0.2 以降に PoC 結果を踏まえて追加する。

### 5.6 ツール名の正規化マップ

| 正規化名 | Claude Code | Codex | OpenCode |
|---|---|---|---|
| `shell.run` | Bash | shell | run_command |
| `file.read` | Read | read_file | read_file |
| `file.write` | Write | write_file | write_file |
| `file.edit` | Edit | - | edit_file |
| `file.glob` | Glob | - | - |
| `file.grep` | Grep | - | - |

`allowed_tools` も正規化名で指定する。adapter 内で agent 固有名に逆変換する。

### 5.7 ストリーミング / cancel

- フレーミング: content-block-level (token-level ではない)
- back-pressure: async generator + 内部バッファ上限 64KB (env で変更可)
- cancel: `agent.cancel(session_id)` -> SDK abort or SIGINT -> 500ms 後 SIGTERM -> SIGKILL

---

## 6. stratoclave-distill (蒸留・検索ライブラリ) 詳細

### 6.1 動機 (claude-smart 影響)

ユーザーが解決したい課題は 3 つ:

1. **compact で会話が失われる**
2. **セッションにゴミが入ると以降が汚染される**
3. **どのセッションが何を担っていたか後から不明**

claude-smart (ReflexioAI) の発想を参考に、raw 会話とは別の「蒸留物レイヤー」を独立 OSS として
切り出す。raw jsonl の全文を毎回参照するのではなく、**purpose / digest / learnings**
として抽出した本質だけを永続化し、新セッションには「関連したものだけ」を inject する。

claude-smart との違い:
- claude-smart は Claude Code / Codex の hook 経由でローカル SQLite に書く個人ツール
- distill は **任意 jsonl を ingest できる汎用ライブラリ**で、session / version / group の
  概念を一級市民として持ち、`source_version` を必ず記録する (再現性と監査性)
- Postgres + pgvector でクラウドスケールにも耐える

### 6.2 形態

- **Pure Python lib** (atelier から in-process import)
- **CLI** (`stratoclave-distill ingest|query|export|gc`)
- **HTTP server** (optional extra, FastAPI)
- **MCP server** (optional, v0.x 後半)
- Docker は不要 (atelier に同梱されて動く想定。スタンドアロン利用は pip install)

### 6.3 公開 API (Python)

```python
from stratoclave_distill import (
    Distiller,           # extract pipeline (Stage 1)
    Curator,             # dedup/merge/supersede (Stage 2)
    Aggregator,          # group rollup (Stage 3)
    Retriever,           # vector + BM25 hybrid search
    ContextPacker,       # token budget 制御で ContextPack 生成
    WatermarkStore,      # session ごとの published_up_to 管理
    SessionPurpose,
    SessionDigest,
    Learning,
    ContextPack,
    DistillerConfig,     # LLM model, embedding provider, DATABASE_URL, ...
)
```

最小利用例:

```python
from stratoclave_distill import Distiller, Retriever, DistillerConfig

cfg = DistillerConfig(
    database_url="postgresql+asyncpg://distill:distill@localhost:5432/distill",
    llm_provider="anthropic",
    llm_model="claude-haiku-4-5-20251001",
    embedding_provider="voyage",       # voyage|openai|cohere|onnx_local
    embedding_model="voyage-3",
)

distiller = Distiller(cfg)
await distiller.ingest_incremental(
    session_id="...",
    group_id="...",
    agent_type="claude_code",
    jsonl_path="/path/to/session.jsonl",
)

retriever = Retriever(cfg)
pack = await retriever.retrieve(
    query="verl GRPO の sbatch でハマった点は?",
    filters={"group_id": "...", "polluted": False},
    budget_tokens=2000,
)
print(pack.to_markdown())
```

### 6.4 データモデル (Postgres + pgvector)

```sql
-- 拡張
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- BM25 補助
-- もしくは tsvector + GIN を採用

-- session の役割と健全性
CREATE TABLE session_purposes (
    session_id      UUID PRIMARY KEY,
    purpose         TEXT NOT NULL,                -- 1-2 行 "verl GRPO の sbatch を作る"
    domain_tags     JSONB NOT NULL DEFAULT '[]'::jsonb,
    success_score   REAL,                         -- 0..1, LLM 自己評価 + 人手で上書き可
    polluted        BOOLEAN NOT NULL DEFAULT false,
    pollution_reason TEXT,
    derived_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    derived_from_version TEXT NOT NULL,            -- atelier の versions.version_id (FK は緩い)
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_session_purposes_polluted ON session_purposes(polluted);
CREATE INDEX idx_session_purposes_tags ON session_purposes USING GIN(domain_tags);

-- session の要約 (RAG 検索用)
CREATE TABLE session_digests (
    digest_id       UUID PRIMARY KEY,
    session_id      UUID NOT NULL,
    version_id      TEXT NOT NULL,
    summary_md      TEXT NOT NULL,
    bm25_text       TEXT NOT NULL,                 -- BM25 / tsvector 用
    bm25_tsv        tsvector GENERATED ALWAYS AS (to_tsvector('simple', bm25_text)) STORED,
    embedding       vector(1024),                  -- 次元は provider に合わせる
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_digests_session ON session_digests(session_id);
CREATE INDEX idx_digests_bm25 ON session_digests USING GIN(bm25_tsv);
CREATE INDEX idx_digests_vec ON session_digests USING hnsw (embedding vector_cosine_ops)
       WITH (m = 16, ef_construction = 64);

-- 個別の学び (claude-smart の skill 相当)
CREATE TABLE learnings (
    learning_id     UUID PRIMARY KEY,
    scope           TEXT NOT NULL,                 -- 'session'|'project'|'group'|'shared'
    project_key     TEXT,
    group_id        UUID,
    rule            TEXT NOT NULL,                 -- "PATH=/opt/slurm/bin:$PATH を入れる"
    why             TEXT NOT NULL,                 -- "OnNodeConfigured で sbatch が見つからない"
    triggers        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {keywords, agent_type, file_globs, cwd_prefix}
    source_session  UUID,
    source_version  TEXT,
    evidence_count  INTEGER NOT NULL DEFAULT 1,
    confidence      REAL NOT NULL DEFAULT 0.5,
    archived_at     TIMESTAMPTZ,
    superseded_by   UUID REFERENCES learnings(learning_id),
    bm25_text       TEXT NOT NULL,                 -- rule + why + triggers を flatten
    bm25_tsv        tsvector GENERATED ALWAYS AS (to_tsvector('simple', bm25_text)) STORED,
    embedding       vector(1024),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_learnings_active ON learnings(scope, archived_at);
CREATE INDEX idx_learnings_group ON learnings(group_id);
CREATE INDEX idx_learnings_bm25 ON learnings USING GIN(bm25_tsv);
CREATE INDEX idx_learnings_vec ON learnings USING hnsw (embedding vector_cosine_ops)
       WITH (m = 16, ef_construction = 64);

-- 増分処理用の watermark
CREATE TABLE distill_watermarks (
    session_id        UUID PRIMARY KEY,
    published_up_to   BIGINT NOT NULL DEFAULT 0,    -- jsonl の seq まで処理済み
    last_run_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- group / project rollup (Stage 3 の出力)
CREATE TABLE group_learnings (
    group_learning_id UUID PRIMARY KEY,
    group_id          UUID NOT NULL,
    summary_md        TEXT NOT NULL,
    contributing_learnings JSONB NOT NULL,           -- learning_id の配列
    embedding         vector(1024),
    bm25_text         TEXT NOT NULL,
    bm25_tsv          tsvector GENERATED ALWAYS AS (to_tsvector('simple', bm25_text)) STORED,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_group_learnings_vec ON group_learnings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_group_learnings_bm25 ON group_learnings USING GIN(bm25_tsv);
```

### 6.5 検索アーキテクチャとパフォーマンス設計

**重要要件**: 検索系として大規模化しても遅くならないよう、最初からスケールパスを意識する。

| 層 | v0.1 既定 | スケール時の差し替え |
|---|---|---|
| Vector ANN | pgvector + HNSW | Qdrant / Milvus / Pinecone へ swap (Retriever interface 経由) |
| BM25 | Postgres tsvector + GIN | OpenSearch / Elasticsearch へ swap |
| Hybrid 融合 | Reciprocal Rank Fusion (RRF, 純 SQL or Python) | リランカー (Cohere Rerank, Voyage Rerank) を後段追加 |
| Embedding 計算 | バッチ化 (32件単位) + provider rate limit 対応 | provider 切替で並列度を上げる |
| LLM 抽出 | Haiku で turn 100 件 = 数秒 | 並列ワーカー数 (env: `DISTILL_WORKERS`) で水平スケール |

**Retriever は ABC で抽象化** (実装差し替え可能):

```python
class VectorIndex(Protocol):
    async def upsert(self, items: list[VectorRecord]) -> None: ...
    async def search(self, query_vec: list[float], k: int, filters: dict) -> list[Hit]: ...

class TextIndex(Protocol):
    async def upsert(self, items: list[TextRecord]) -> None: ...
    async def search(self, query: str, k: int, filters: dict) -> list[Hit]: ...

class Retriever:
    def __init__(self, vec: VectorIndex, txt: TextIndex, fuser: Fuser): ...
```

既定実装は Postgres ベースだが、`PgVectorIndex` / `PgBm25Index` を `QdrantIndex` /
`OpenSearchIndex` に差し替え可能にする。

**index 戦略**:
- HNSW の `m`, `ef_construction` は env (`DISTILL_HNSW_M=16`, `DISTILL_HNSW_EFC=64`)
- 検索時の `ef_search` は query 時に動的指定 (env 既定 `DISTILL_HNSW_EF=64`)
- BM25 は GIN index、tsvector の生成カラム化で挿入時に build 済み
- 大規模化対応: パーティショニング (group_id でハッシュパーティション) を v0.2 以降で追加

**キャッシング**:
- ContextPack は (query_hash, filter_hash, budget) をキーに LRU + TTL
- embedding は (text_hash, model) をキーに永続キャッシュ (`distill_embedding_cache` テーブル)

### 6.6 抽出タイミング

3 段階の組み合わせ (前回確定):

1. **Auto (turn-based)**: turn 数が `DISTILL_AUTO_TURNS` (既定 20) を超えるごとに増分 extract
2. **On finalize**: session の version が freeze された時に最終 extract
3. **Manual**: `distill ingest --immediate` or atelier から `/learn` API で即時実行

watermark を持つので 1 と 2 の重複は冪等。`distill_watermarks.published_up_to` を超えた seq のみ
処理する。

### 6.7 Curator (dedup / merge / supersede)

```
新 learning L_new を受領
  -> L_new と group_id / project_key が一致する既存 learnings の上位 K を
     vector + BM25 hybrid で取得 (K=10)
  -> 各候補について cosine_sim(L_new, L_existing) を計算
     - sim >= τ_merge (既定 0.95): MERGE (rule / why を LLM で統合、evidence_count++)
     - sim >= τ_conflict (既定 0.80) かつ意味的に矛盾: SUPERSEDE
       (LLM 判定、L_existing.archived_at = now, L_existing.superseded_by = L_new)
     - それ以外: 新規 INSERT
```

LLM 判定は重い場合があるため、`Curator` は非同期キューでバッチ処理する。閾値は env で調整可能
(`DISTILL_TAU_MERGE`, `DISTILL_TAU_CONFLICT`)。

### 6.8 ContextPacker (token budget 制御)

```
入力: hybrid 検索結果 (learnings + digests + group_learnings)、budget_tokens
処理:
  1. group_learnings を最優先 (1 件あたり ~150 token)
  2. learnings (active のみ) を confidence * recency でランク
  3. session_digests を query との類似度順
  4. tiktoken / Anthropic tokenizer で正確に計測
  5. budget を超える前に切り、Markdown としてフォーマット
出力: ContextPack { markdown, items, total_tokens, source_ids }
```

ContextPack には source_ids を含め、atelier 側で「このコンテキストはどの過去 session 由来か」
をユーザーに表示できる。

### 6.9 pollution mark

- `session_purposes.polluted = true` のセッションは Retriever のフィルタで除外 (既定)
- 蒸留時に LLM が「この session は途中で目的が逸脱した / エラー連鎖した」と判断したら
  `polluted_reason` 付きで自動マーク (heuristic + LLM)
- ユーザーは UI / CLI から手動で polluted を上書き可能

### 6.10 環境変数 (distill 設定)

| 変数 | 用途 | 既定 |
|---|---|---|
| `DATABASE_URL` | Postgres 接続 (atelier と相乗り or 別 DB) | (必須) |
| `DISTILL_LLM_PROVIDER` | `anthropic` / `openai` | `anthropic` |
| `DISTILL_LLM_MODEL` | LLM model id | `claude-haiku-4-5-20251001` |
| `DISTILL_LLM_BASE_URL` | stratoclave 経由なら設定 | (空) |
| `DISTILL_LLM_API_KEY` | LLM API key | (必須) |
| `DISTILL_EMBEDDING_PROVIDER` | `voyage` / `openai` / `cohere` / `onnx_local` | `voyage` |
| `DISTILL_EMBEDDING_MODEL` | embedding model | `voyage-3` |
| `DISTILL_EMBEDDING_DIM` | 次元 (Postgres カラムと整合) | `1024` |
| `DISTILL_AUTO_TURNS` | 自動抽出する turn 閾値 | `20` |
| `DISTILL_WORKERS` | 並列抽出ワーカー数 | `2` |
| `DISTILL_HNSW_M` | HNSW build パラメータ | `16` |
| `DISTILL_HNSW_EFC` | HNSW build パラメータ | `64` |
| `DISTILL_HNSW_EF` | HNSW search パラメータ | `64` |
| `DISTILL_TAU_MERGE` | merge 閾値 (cosine) | `0.95` |
| `DISTILL_TAU_CONFLICT` | conflict 閾値 (cosine) | `0.80` |
| `DISTILL_RRF_K` | RRF の定数 | `60` |
| `DISTILL_CONTEXT_BUDGET_DEFAULT` | デフォルト budget tokens | `2000` |

### 6.11 過去 agent 問い合わせの 3 モードへの貢献

atelier の SnapshotRuntime は 3 モードを切り替える:

| モード | 中身 | 担当 |
|---|---|---|
| `full` | jsonl 全文を resume | loom |
| `digest` | session_purpose + summary を context として inject | distill (export) |
| `learnings_only` | 関連 learnings のみ inject | distill (export) |

これによりトークン効率と監査性が両立し、コスト・スピードで優位に立てる。

---

## 7. stratoclave-atelier (上層アプリ) 詳細

### 7.1 起動形態

- **配布**: Docker イメージ (`ghcr.io/littlemex/stratoclave-atelier:vX.Y.Z`)
- **ローカル起動**: `docker compose up` (finch でも動くこと)
- **クラウド**: 同一イメージを ECS / Fargate / k8s に置けば動く。IaC は本 OSS スコープ外
- **DB**: Postgres を必須。compose に同梱 + `DATABASE_URL` で外部 DB に差し替え可能
- **ボリューム**: jsonl blob 保存先 (`/var/lib/atelier/blobs`) を host volume mount

### 7.2 docker-compose.yml (最小構成)

```yaml
services:
  atelier:
    image: ghcr.io/littlemex/stratoclave-atelier:latest
    ports:
      - "8765:8765"
    environment:
      DATABASE_URL: postgresql+asyncpg://atelier:atelier@db:5432/atelier
      STRATOCLAVE_BASE_URL: ${STRATOCLAVE_BASE_URL:-}
      STRATOCLAVE_API_KEY: ${STRATOCLAVE_API_KEY:-}
      ATELIER_AUTH_MODE: ${ATELIER_AUTH_MODE:-none}  # none|bearer|stratoclave_cognito
      ATELIER_BLOB_DIR: /var/lib/atelier/blobs
    volumes:
      - blobs:/var/lib/atelier/blobs
    depends_on:
      - db

  db:
    image: postgres:16
    environment:
      POSTGRES_USER: atelier
      POSTGRES_PASSWORD: atelier
      POSTGRES_DB: atelier
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  blobs:
  pgdata:
```

### 7.3 データモデル (Postgres)

```sql
-- セッションのグループ化単位
CREATE TABLE groups (
    group_id    UUID PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    labels      JSONB NOT NULL DEFAULT '[]'::jsonb,
    parent_id   UUID REFERENCES groups(group_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_groups_parent ON groups(parent_id);

-- エージェント実行の一単位
CREATE TABLE sessions (
    session_id    UUID PRIMARY KEY,
    group_id      UUID REFERENCES groups(group_id),
    agent_type    TEXT NOT NULL,
    cwd           TEXT NOT NULL DEFAULT '',
    head_version  TEXT REFERENCES versions(version_id) DEFERRABLE INITIALLY DEFERRED,
    archived      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_sessions_group ON sessions(group_id);
CREATE INDEX idx_sessions_archived ON sessions(archived);

-- JSONL の immutable snapshot (DAG)
CREATE TABLE versions (
    version_id    TEXT PRIMARY KEY,         -- sha256(canonical_jsonl)[:40]
    session_id    UUID NOT NULL REFERENCES sessions(session_id),
    parent_id     TEXT REFERENCES versions(version_id),
    blob_path     TEXT NOT NULL,            -- ATELIER_BLOB_DIR からの相対
    byte_length   BIGINT NOT NULL,
    agent_type    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    frozen_at     TIMESTAMPTZ,              -- NULL=live, NOT NULL=immutable
    label         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_versions_session ON versions(session_id, created_at);
CREATE INDEX idx_versions_parent ON versions(parent_id);

-- 検索/表示用に抽出したイベント
CREATE TABLE events (
    event_id      UUID PRIMARY KEY,
    version_id    TEXT NOT NULL REFERENCES versions(version_id),
    session_id    UUID NOT NULL,
    seq           BIGINT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    raw_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_version ON events(version_id, seq);
CREATE INDEX idx_events_session ON events(session_id, occurred_at);

-- 過去 agent 問い合わせの監査用ログ
CREATE TABLE snapshot_queries (
    query_id          UUID PRIMARY KEY,
    parent_session_id UUID NOT NULL,
    queried_session   UUID NOT NULL,
    queried_version   TEXT NOT NULL REFERENCES versions(version_id),
    prompt            TEXT NOT NULL,
    response          TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);
```

### 7.4 バージョニングセマンティクス

- 各 `Version` は単一の `parent_id` を持つ DAG (merge / branch は v1 で持たない)
- `frozen_at` NULL = ライブ、NOT NULL = immutable (ファイル系統も `chmod 0444`)
- 新 turn 追加 = 新 `Version` 作成 + `parent_id` 連鎖
- ユーザー明示の "freeze" のみ `frozen_at` をセット (自動 freeze は v1 では持たない)
- fork = 任意の version を parent にして新セッションを作成 (cross-agent fork は不可、同 agent のみ)

### 7.5 Cross-agent JSONL の扱い

- **コンテキスト参照のみ対応**: 異 agent の jsonl をテキストとして読むことは可能
- **完全再生 (tool call の再実行) は非対応**: tool 名/引数の正確な変換が困難なため
- `AgentBackend.normalize()` がテキスト化して `events.content` に入る
- 過去 agent を再起動するときは元と同じ agent で起動する原則

### 7.6 公開 API

```
# REST
GET    /api/groups
POST   /api/groups
GET    /api/groups/{id}/sessions
PUT    /api/sessions/{id}                     # group_id 紐付け等
GET    /api/sessions
GET    /api/sessions/{id}/versions
GET    /api/sessions/{id}/versions/{vid}
POST   /api/sessions/{id}/versions/{vid}/freeze
POST   /api/sessions/{id}/versions/{vid}/fork
POST   /api/sessions/{id}/messages            # メイン agent への送信
POST   /api/sessions/{id}/cancel
POST   /api/sessions/{id}/versions/{vid}/query    # 過去 agent 問い合わせ
GET    /api/sessions/{id}/versions/{v1}/diff/{v2}

# WebSocket (channel multiplex)
WS     /ws/events?session_id=...
       channels: "events" | "terminal" | "snapshot_stream"

# SSE
SSE    /sse/scan                              # 新規セッション検出通知
```

### 7.7 環境変数 (atelier 設定)

| 変数 | 用途 | 既定 |
|---|---|---|
| `DATABASE_URL` | Postgres 接続 | `postgresql+asyncpg://...` |
| `STRATOCLAVE_BASE_URL` | agent に渡す `ANTHROPIC_BASE_URL` | (空=直接 Anthropic) |
| `STRATOCLAVE_API_KEY` | agent に渡す `ANTHROPIC_AUTH_TOKEN` | (空) |
| `ATELIER_AUTH_MODE` | `none` / `bearer` / `stratoclave_cognito` | `none` |
| `ATELIER_AUTH_TOKEN` | bearer 時のトークン | - |
| `ATELIER_BLOB_DIR` | jsonl blob 保存先 | `/var/lib/atelier/blobs` |
| `ATELIER_LISTEN_HOST` | bind host | `0.0.0.0` |
| `ATELIER_LISTEN_PORT` | bind port | `8765` |
| `ATELIER_BUFFER_SIZE` | streaming back-pressure | `65536` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel 出力先 (任意) | (空=無効) |

ハードコード絶対禁止 (グローバルルール)。すべて env 経由で注入。

### 7.8 認証モード

- **`none`**: ローカル個人運用、認証なし (既定)
- **`bearer`**: `ATELIER_AUTH_TOKEN` を Bearer ヘッダで検証
- **`stratoclave_cognito`**: stratoclave の Cognito JWT を検証 (extra として実装)

### 7.9 distill 連携

- atelier は `stratoclave-distill` を **pip 依存** で持ち、in-process import で利用する
- distill の Postgres 接続は **atelier と同 DB を相乗り** が既定 (テーブル接頭辞で分離)
- 別 DB 化したい場合は `DISTILL_DATABASE_URL` を別途設定
- メイン session 送信時に `distill.retrieve()` -> ContextPack -> system context 注入
- turn 完了 / freeze 時に `distill.ingest_incremental()` を非同期で発火
- UI に「このセッションの purpose / digest / learnings」タブを設け distill の検索を表示

### 7.10 過去 agent 問い合わせの 3 モード切替

UI のスナップショット問い合わせモーダルで以下の 3 モードを選択可能:

- `full`: jsonl 全文 (loom)
- `digest`: distill の session_purpose + summary を context に inject (token 節約)
- `learnings_only`: distill の関連 learnings のみ inject (最軽量)

既定は `digest`。UI に token 概算と料金概算を表示。

---

## 8. claude-capture からの移行

### 8.1 移行戦略

| Phase | 期間 | 内容 |
|---|---|---|
| 0 | 1-2 週 | atelier の skeleton + loom Walking Skeleton (Claude Code only) |
| 1 | 2-4 週 | atelier の REST が claude-capture の `/api/*` 互換シグネチャを返す。`web_server.py` 並走 |
| 2 | 3-6 週 | claude-capture の static HTML を atelier に移植、新機能 (group/version) UI 追加 |
| 3 | 1 週 | claude-capture 廃止、`data.db` を atelier へマイグレーション後アーカイブ |

### 8.2 SQLite -> Postgres マイグレーション

claude-capture の既存テーブル対応:

| 旧 (SQLite) | 新 (Postgres) | 備考 |
|---|---|---|
| `messages` | `events` | uuid をキーに upsert (冪等) |
| `session_metadata` | `sessions` | name / archived / cwd を引き継ぎ |
| `code_blocks`, `file_ops`, `assistant_texts` | derived | バージョン管理対象として再計算推奨 |
| `file_snapshots` | `versions` | 直接対応、blob 移動 |
| `memos` | (別テーブル) | 付箋機能として独立 |
| `remote_instances`, `remote_sessions` | スコープ外 | 移行しない |

`/Users/akazawt/works/data-science/claudecode/claude-capture/parser.py` の ANSI パース機能は、
loom の adapter が ACP で構造化済みのため不要。raw PTY しか出さない backend が将来出た
場合のみ atelier 内に optional モジュールとして復活させる。

### 8.3 既存実装の参照ファイル

- `/Users/akazawt/works/data-science/claudecode/claude-capture/web_server.py` (3724 行)
- `/Users/akazawt/works/data-science/claudecode/claude-capture/db.py` (1348 行)
- `/Users/akazawt/works/data-science/claudecode/claude-capture/reader.py` (457 行)
- `/Users/akazawt/works/data-science/claudecode/claude-capture/parser.py` (216 行)
- `/Users/akazawt/works/data-science/claudecode/claude-capture/static/{index,live,board,remote-live}.html`

---

## 9. ロードマップ

### 9.1 stratoclave-loom

| ver | 内容 |
|---|---|
| 0.1 | Claude Code adapter, stdio, ACP 最小、Python API、CLI 最小版、pytest CI |
| 0.2 | OpenCode adapter (PoC 後)、normalize 拡充、ProcessPool 最適化 |
| 0.3 | Kiro / Codex adapter, allowed_tools 制約, resume_from_jsonl |
| 1.0 | API freeze, semver |

### 9.2 stratoclave-distill

| ver | 内容 |
|---|---|
| 0.1 | Distiller (Stage1) + Curator (Stage2) 最小、Postgres + pgvector、HNSW index、Anthropic LLM、Voyage embedding、CLI (`ingest`/`query`/`export`) |
| 0.2 | Aggregator (Stage3) で group rollup、ContextPacker の token budget 厳密化、ONNX local embedding を optional extra に |
| 0.3 | HTTP server (FastAPI) optional extra、検索パフォーマンス最適化 (キャッシュ、partition)、reranker 接続 |
| 0.4 | MCP server optional extra (任意 MCP 対応 agent から `recall_past_learnings` ツール提供) |
| 1.0 | API freeze、semver、index swap (Qdrant/OpenSearch) のリファレンス実装 |

### 9.3 stratoclave-atelier

| ver | 内容 |
|---|---|
| 0.1 | REST + WS + SSE 骨格、Postgres、group/session の CRUD、loom 0.1 連携、Docker compose |
| 0.2 | JSONL バージョン管理 (freeze/fork/diff)、UI のタイムライン、distill 0.1 連携 (ContextPack 注入と increment ingest) |
| 0.3 | SnapshotRuntime、Cross-Session RPC Hub、過去 agent 問い合わせ UI、distill の 3 モード切替 (full/digest/learnings_only) |
| 1.0 | claude-capture 完全リプレース、OpenTelemetry optional |

### 9.4 stratoclave (既存)

直接の変更なし。atelier から使う際の API key 払い出し / role 設定は既存機能で対応。

---

## 10. リポジトリ配置

```
/Users/akazawt/stratoclave-oss/        既存 (stratoclave)
/Users/akazawt/stratoclave-loom/       新規 (Pure Python lib, Apache 2.0)
/Users/akazawt/stratoclave-distill/    新規 (Pure Python lib + CLI, Apache 2.0)
/Users/akazawt/stratoclave-atelier/    新規 (Web app + UI + Docker, Apache 2.0)
```

依存関係:
- `stratoclave-atelier` -> **`stratoclave-loom` と `stratoclave-distill` を pip 依存**
- `stratoclave-loom` -> 依存なし (Pure Python)
- `stratoclave-distill` -> 依存なし (Pure Python、Postgres は外部接続)
- `stratoclave-loom` <-> `stratoclave-distill`: **互いを知らない** (atelier が両者を組み合わせる)
- どの新 OSS も stratoclave への直接依存は持たない (env 経由のみ)
- monorepo にはしない (stratoclave AWS IaC と Python lib の混在を避ける)

GitHub:
- `littlemex/stratoclave` (既存)
- `littlemex/stratoclave-loom` (新規)
- `littlemex/stratoclave-distill` (新規)
- `littlemex/stratoclave-atelier` (新規)

PyPI:
- `stratoclave-loom`
- `stratoclave-distill`
- `stratoclave-atelier` (Docker 配布が一次、pip も提供可)

Docker:
- `ghcr.io/littlemex/stratoclave-atelier` (atelier のみ)

---

## 11. 設計判断のサマリ (確定済み)

| 項目 | 判断 |
|---|---|
| シリーズ名階層 | clave -> loom -> distill -> atelier |
| 中層 (agent 抽象化) | **stratoclave-loom** |
| 中層 (蒸留・検索) | **stratoclave-distill** (claude-smart 影響、独立 OSS) |
| 上層 (UI + DB + orchestrator) | **stratoclave-atelier** |
| 実装言語 | **Python 3.11+** (3 OSS とも) |
| ライセンス | Apache 2.0 (stratoclave 揃え) |
| sandbox 強度 | **Soft Guard のみ** (chmod 0444 + allowed_tools 制限) |
| Docker / IaC (loom) | **持たない**。Pure Python lib |
| Docker / IaC (distill) | **持たない**。Pure Python lib + CLI、HTTP/MCP server は optional extra |
| Docker / IaC (atelier) | **Docker 必須**。docker compose (finch 互換)。IaC 不要 |
| DB (atelier / distill) | **Postgres + pgvector**。SQLite は不採用。`DATABASE_URL` で差し替え可能 |
| 検索パフォーマンス | **swap 可能な VectorIndex / TextIndex 抽象**。pgvector + tsvector 既定、Qdrant/OpenSearch へ差し替え可能 |
| Embedding | クラウド既定 (Voyage 等)、ONNX local は optional extra |
| LLM 抽出 | Anthropic Haiku 既定、stratoclave 経由 OK |
| 抽出タイミング | turn 数閾値 (auto) + freeze (on-finalize) + 手動 /learn の 3 段 (watermark で冪等) |
| MCP server | distill v0.4 で導入 (後回し) |
| repo 配置 | **別 repo** 4 つ (monorepo にしない) |
| stratoclave 連携 (loom/distill) | **直接知らない**。LLM 呼び出しの env のみ通す |
| stratoclave 連携 (atelier) | **env 経由で agent に注入のみ**。直接 HTTP しない (auth 委譲を除く) |
| cross-agent replay | **コンテキスト参照のみ**。tool call 完全再生は非対応 |
| 過去 agent 通信モデル | **atelier が orchestrator として両者を仲介** + **3 モード切替** (full / digest / learnings_only) |
| 自動 freeze | **v1 では持たない**。ユーザー明示 pin のみ |
| memos 機能 | **スコープ外**。別 OSS or 別テーブル分離 |
| UI | **atelier に同梱**。claude-capture から移植 |
| WebSocket / SSE | **両方使う**。WS は streaming、SSE は scan 通知 |
| Auth (atelier) | **none / bearer / stratoclave_cognito の 3 モード**。既定 none |

---

## 12. 未解決論点 (実装中に決める)

1. **OpenCode / Kiro / Codex の ACP 対応実態**: loom v0.2 / v0.3 着手前に PoC 必須
2. **`requestPermission` の同期 / 非同期**: ACP 仕様確認後に決定 (loom 内部の queue 設計に影響)
3. **JSONL freeze の自動化**: v1 はユーザー明示のみ。v2 以降で「N turn ごとに自動 freeze」検討
4. **Postgres スキーマの semver 戦略**: alembic を atelier / distill それぞれ v0.1 から導入する
5. **memos 機能の置き場**: atelier 同梱 vs 別 OSS。決定保留
6. **WebSocket terminal channel**: ACP 構造化があれば不要になる可能性。v0.1 では実装しない方針
7. **distill の Curator LLM 判定コスト**: 矛盾判定を毎件 LLM 呼び出しにするか、cosine + heuristic で前段足切り後に LLM。v0.1 でベンチマーク必須
8. **distill の Embedding model 切替時の再 index**: dim が変わる場合の reindex フロー (alembic + バックフィル job)
9. **pollution 自動判定の精度**: heuristic + LLM の 2 段。v0.1 はユーザー手動マークのみで開始、v0.2 で自動推論を追加
10. **atelier と distill の DB 相乗り vs 別建て**: 既定は相乗り、運用で分けたい場合は `DISTILL_DATABASE_URL`。スキーマ衝突を避けるため distill 側のテーブルは prefix `distill_` を内部規約で運用

---

## 13. キャッチフレーズ

- 英 (シリーズ全体): *"Weave every agent. Distill every session. Forget nothing."*
- 日 (シリーズ全体): *「agent を編み、会話を蒸留し、忘れない。」*
- loom 単体: *"One protocol, every coding agent."*
- distill 単体: *"Your past sessions, queryable forever."*
- atelier 単体: *"The studio where loom and distill meet."*

---

## 14. 既知の参考資料

- ACP (Agent Client Protocol, Zed): 仕様は draft、実装時に最新版を確認
- stratoclave: https://github.com/littlemex/stratoclave
- claude-smart (ReflexioAI, distill の発想源): https://github.com/ReflexioAI/claude-smart
  ・skills / preferences の分離永続化、watermark、hybrid search、自己修正パイプラインを参照
  ・**コードはコピーしない。思想のみ取り込み**、README で帰属を明示する
- claude-capture (移行元): `/Users/akazawt/works/data-science/claudecode/claude-capture/`
- pgvector: https://github.com/pgvector/pgvector
- Reciprocal Rank Fusion: Cormack, Clarke, Buettcher (2009)

---

**この設計書は v0.1 着手時点のベースライン。実装で得た知見はこのファイルへの追記または
ADR (`docs/adr/NNNN-*.md`) として記録すること。**
