# codex-driven-dev-platform

プロンプトから必要なプロジェクトエントリを計画し、競合しないリースを取得して、
実行固有のCodex権限でタスクを起動するためのオーケストレーション層です。

## 構成

- `TaskPlanner`：プロンプトとリース判断理由を扱い、再考方針を決定する。
- `EntryLeasePlanner`：`entry-lease-planner`を起動し、同じ
  `EntryLeasePlan`をTaskPlannerとTaskSchedulerへ渡す。
- `TaskScheduler`：`EntryManager`への唯一の申請者。競合Planの待ち行列、
  再申請、TaskExecutor起動、終了後のリース解放を担当する。
- `EntryManager`：glob・symlink・予約領域を機械的に検証し、`flock`中に
  リースを原子的に記録する。
- `CodexTaskExecutor`：事前に`LEASED`となったPlanだけを、完全な実行固有権限で
  実行する。リース申請・解放は行わない。

待ち行列には、直近の申請結果が競合による`WAITING`だったPlanだけが入ります。
blocker executorの終了は再申請の契機であり、再申請が`LEASED`になった場合だけ
新しいTaskExecutorが起動します。

詳細は[アーキテクチャ文書](docs/architecture.md)を参照してください。

## 必要要件

- Python 3.14以上
- `uv`
- 利用可能で認証済みのCodex CLI
- Linuxでは`bubblewrap`（`bwrap`コマンド）

書き込みを伴うTaskExecutorは、CodexのLinux sandboxが内部mountに使う`/tmp`を
executor専用の一時ファイルシステムへ分離するため、外側でも`bwrap`を使用します。
ホストの共有`/tmp`はTaskExecutorへ公開されません。

## インストール

リポジトリ内で仮想環境を準備すると、console scriptも同時に利用可能になります。

```console
$ uv sync
$ uv run codex-driven-dev-platform --help
```

別の環境へツールとしてインストールする場合は、リポジトリを指定して`uv tool install`
を実行できます。

```console
$ uv tool install /path/to/codex-driven-dev-platform
$ codex-driven-dev-platform --help
```

## CLIでタスクを実行する

プロンプトと対象プロジェクトを指定します。

```console
$ uv run codex-driven-dev-platform run \
    -C /path/to/project \
    "READMEにインストール方法を追加してください。"
```

標準入力またはUTF-8ファイルからプロンプトを渡すこともできます。

```console
$ printf '%s\n' 'テストを追加してください。' | \
    uv run codex-driven-dev-platform run -C /path/to/project

$ uv run codex-driven-dev-platform run \
    -C /path/to/project \
    --prompt-file task.txt
```

代表的なオプションは以下です。

- `--model MODEL`：TaskExecutorが使用するモデル
- `--planner-model MODEL`：エントリ計画エージェントが使用するモデル
- `--timeout SECONDS`：TaskExecutorのタイムアウト
- `--planner-timeout SECONDS`：各エントリ計画試行のタイムアウト
- `--max-generations N|unlimited`：再考するPlan世代数（既定8）
- `--no-ephemeral`：実行セッションを永続化する
- `--json`：実行結果、Plan、判断履歴を単一JSONとして出力する
- `--debug`：schedulerの状態をstderrへリアルタイム表示する
- `--codex PATH`：使用するCodex CLI実行ファイルを指定する

自動処理では`--json`を指定できます。正常終了時は0、Codexが非ゼロで終了した
場合はその終了コード、実行タイムアウト時は124、リース解放失敗時は74を返します。

```console
$ uv run codex-driven-dev-platform run \
    -C /path/to/project \
    --json \
    --timeout 300 \
    "型チェックエラーを修正してください。"
```

デバッグ表示とJSONは別ストリームへ出力されるため、併用できます。

```console
$ uv run codex-driven-dev-platform run \
    -C /path/to/project \
    --debug --json \
    "型チェックエラーを修正してください。"
```

stderrが端末の場合は、task状態、世代ごとのPlan、read/writeエントリ数、待ち行列、
直近のイベントを含むTUI風ダッシュボードがその場で更新されます。リダイレクトや
パイプでstderrが端末ではない場合は、`plan-created`、`lease-decision`、
`executor-started`、`execution-finished`などを追記形式のログとして出力します。
JSONはどちらの場合もstdoutだけに出力されます。

現在有効なリースは`leases`で確認できます。

```console
$ uv run codex-driven-dev-platform leases -C /path/to/project
$ uv run codex-driven-dev-platform leases -C /path/to/project --json
```

`run_task.py`も同じCLIへのラッパーなので、次の形式で利用できます。

```console
$ uv run python run_task.py run -C /path/to/project "タスク内容"
```

## Python APIで使用する

```python
from codex_driven_dev_platform import (
    CodexEntryLeasePlanningAgent,
    CodexTaskExecutor,
    EntryLeasePlanner,
    EntryManager,
    TaskPlanner,
    TaskScheduler,
)

project = "/path/to/project"
manager = EntryManager(project)
executor = CodexTaskExecutor(manager)
scheduler = TaskScheduler(manager, executor)
agent = CodexEntryLeasePlanningAgent(project)
lease_planner = EntryLeasePlanner(agent, scheduler)
planner = TaskPlanner(lease_planner, scheduler)

request, initial_plan = planner.submit(
    "Implement the requested change.",
    executor_options={"timeout": 300, "ephemeral": True},
)
execution = scheduler.wait(request.task_id)

lease_planner.shutdown()
scheduler.shutdown()
```

`TaskPlanner`は競合・不正時に自動で再考を要求し、既定では最大8世代まで試します。
`max_generations=None`で上限を外すか、`reconsideration_policy`で独自方針を指定できます。

`CodexEntryLeasePlanningAgent`は、Codex CLIを読み取り専用で起動し、
`entry_lease_plan.schema.json`に従うJSONファイルを生成します。テストや別の
エージェント基盤では`EntryLeasePlanningAgent`プロトコルを実装して差し替えられます。
Codexのfilesystem権限に合わせ、エントリには完全パスまたは末尾が`/**`の
ディレクトリサブツリーだけを使用できます。それ以外のglobは不正Planとして再考されます。

## リースレジストリと復旧

有効なリースはプロジェクト直下の`.task-executor-leases.jsonl`へ記録されます。
更新時だけファイルを開いて`flock`を取得し、追記・`fsync`後に閉じます。

PIDから古いリースを自動回収しません。親プロセスがクラッシュした場合は、すべての
TaskExecutorとSchedulerが停止していることを確認してから、このファイルを削除して
ください。
