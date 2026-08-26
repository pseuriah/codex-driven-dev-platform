# Architecture

## 不変条件

1. `TaskScheduler`だけが`EntryManager.acquire()`を呼ぶ。
2. 待ち行列には、直近の判断が競合による`WAITING`のPlanだけが存在する。
3. `TaskExecutor`は`LEASED`のPlanでのみ起動し、リースを再申請しない。
4. 同じ`task_id`と`executor_id`には、それぞれ最大一つの有効リースしか存在しない。
5. blockerの終了は再申請の契機であり、起動許可そのものではない。
6. 待機Planと再考Planのうち、最初に`LEASED`となった一つだけを実行する。

## 初回申請

```mermaid
sequenceDiagram
    participant TP as TaskPlanner
    participant ELP as EntryLeasePlanner
    participant Agent as entry-lease-planner
    participant TS as TaskScheduler
    participant EM as EntryManager
    participant DB as LeaseRegistry
    participant TE as TaskExecutor

    TP->>ELP: TaskRequest
    ELP->>Agent: Plan生成要求
    Agent-->>ELP: EntryLeasePlan
    ELP-->>TP: 同じPlanを返す
    ELP->>TS: 同じPlanを送る
    TS->>EM: Planを申請
    EM->>DB: ロック中に検証

    alt 承認
        EM->>DB: LEASEDを記録
        EM-->>TS: LEASED
        TS-->>TP: 承認結果
        TS->>TE: Planで起動
    else 競合
        EM-->>TS: WAITINGとblocker
        TS->>TS: 待ち行列へ追加
        TS-->>TP: 競合理由
        TP->>ELP: 再考要求
        ELP->>Agent: 別Plan生成要求
    else 不正
        EM-->>TS: INVALIDと理由
        TS-->>TP: 違反理由
        TP->>ELP: 再考要求
    end
```

## blocker終了時の再申請

```mermaid
sequenceDiagram
    participant Running as RunningTaskExecutor
    participant TS as TaskScheduler
    participant EM as EntryManager
    participant DB as LeaseRegistry
    participant TP as TaskPlanner
    participant ELP as EntryLeasePlanner
    participant Agent as entry-lease-planner
    participant Next as NewTaskExecutor

    Running-->>TS: executor終了
    TS->>EM: active Planを解放
    EM->>DB: RELEASEDを記録
    TS->>TS: blockerに紐づくWAITING Planを抽出
    TS->>EM: 同じPlanを再申請
    EM->>DB: 競合を再検証

    alt 承認
        EM-->>TS: LEASED
        TS->>TS: 待ち行列から削除
        TS->>ELP: 再考中止要求
        ELP->>Agent: 停止要求
        TS->>Next: Planで起動
    else 依然競合
        EM-->>TS: WAITINGと新しいblocker
        TS->>TS: 待ち行列を更新
        TS-->>TP: 更新された競合理由
    else 不正化
        EM-->>TS: INVALID
        TS->>TS: 待ち行列から削除
        TS-->>TP: 違反理由
    end
```

## オブジェクト図

```mermaid
flowchart LR
    TP["taskPlanner : TaskPlanner"]
    Task["taskRequest : TaskRequest"]
    ELP["leasePlanner : EntryLeasePlanner"]
    Agent["plannerAgent : entry-lease-planner"]
    PlanA["planA : EntryLeasePlan WAITING"]
    PlanB["planB : EntryLeasePlan PROPOSED"]
    TS["taskScheduler : TaskScheduler"]
    Queue["queueItem : WaitRegistration"]
    EM["entryManager : EntryManager"]
    DB["leaseRegistry : LeaseRegistry"]
    ActivePlan["activePlan : EntryLeasePlan LEASED"]
    Running["runningExecutor : TaskExecutor RUNNING"]

    TP -->|owns| Task
    TP -->|requests reconsideration| ELP
    ELP -->|starts| Agent
    ELP -->|returns plan| TP
    ELP -->|submits plan| TS
    Task -->|owns| PlanA
    Task -->|owns| PlanB
    Agent -->|creates| PlanB
    TS -->|owns| Queue
    Queue -->|references| PlanA
    Queue -->|blocked by| Running
    TS -->|sole applicant| EM
    EM -->|uses| DB
    DB -->|contains| ActivePlan
    ActivePlan -->|assigns entries| Running
```

## クラス図

```mermaid
classDiagram
    class TaskPlanner
    class TaskRequest
    class EntryLeasePlanner
    class EntryLeasePlanningAgent
    class EntryLeasePlan
    class EntryManager
    class LeaseRegistry
    class TaskScheduler
    class WaitRegistration
    class TaskExecutor

    TaskPlanner --> TaskRequest : creates
    TaskPlanner --> EntryLeasePlanner : delegates
    EntryLeasePlanner --> EntryLeasePlanningAgent : starts
    EntryLeasePlanner --> EntryLeasePlan : creates
    TaskRequest "1" *-- "1..*" EntryLeasePlan : owns
    TaskScheduler --> EntryManager : sole applicant
    EntryManager --> LeaseRegistry : persists
    EntryManager --> EntryLeasePlan : updates status
    TaskScheduler "1" o-- "*" WaitRegistration : manages
    WaitRegistration --> EntryLeasePlan : references
    TaskScheduler --> TaskExecutor : launches
    TaskExecutor --> EntryLeasePlan : consumes
```
