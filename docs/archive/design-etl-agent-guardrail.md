# ETL-Agent 控制面（Guardrail 版）— 开发规格书

> 对应《项目实战》文档 **2.8 生产级 ETL-Agent 数据集成平台** 的裁剪实现。
> 版本：v0.1（待 YG 评审） · 定位：简历项目 · 开发窗口：约 4 周

---

## 0. 一句话定义

**让 Agent 能改生产数据，但只允许它按审批过的那一次、那一刻、那一版本、在预算内改一次。**

这个项目不是"用 LLM 生成 ETL 配置"，而是**给"AI 会动真实数据"这件事装上不可绕过的护栏**。生成能力只占 30%，剩下的 70% 是授权、幂等、防重放、可审计。

### 0.1 为什么是它（选型结论，供简历叙事对照）

现有六项能力（合同审批 2.4 / 客服微调 / RAG / 舆情 / 问数 / 多智能体雏形）全部落在
**"业务规则 + 提示词与数据调优"** 这一层。面试官向上追问一层就会撞墙：

| 面试官可能的追问 | 现有项目是否能接住 |
| --- | --- |
| 审批的人和提交的人不是同一个，你怎么保证？ | 合同审查只有"辅助审查"定位，无执行授权 |
| 大模型多写了一次会怎样？ | 无副作用执行链 |
| 你说微调提升了效果，业务真的变好了吗？ | 有离线指标，无运行时治理 |
| 你怎么证明 AI 干的事没被人事后改过？ | 无证据链 |

ETL-Agent 的护栏内核正好补上这一层，而且与合同审批形成**业务语义 → 执行协议**的连续叙事。

---

## 1. 范围

### 1.1 P0：真做，做成能被追问到底的

| # | 模块 | 必须达到的可验证标准 |
| --- | --- | --- |
| 1 | 认证与角色 | 多用户；项目内分配 申请人/数据审批人/安全审批人/操作员/审计 五种角色槽 |
| 2 | 数据源与元数据探查 | 接入 SQLite / MySQL / CSV 三类；只读探查 schema、主键、近似行数、**脱敏样本**（邮箱/手机/身份证打码）；探查结果算 `profile_digest` |
| 3 | 需求 → EtlPlan | 自然语言 → 结构化 EtlPlan JSON + 目标执行器配置；**LLM 为可选适配层，缺失时走模板+规则也要能跑通全链路** |
| 4 | 确定性门禁 | 纯代码校验器，不依赖模型：语法、字段存在性、类型兼容、主键冲突、**P0 危险操作检测**（DROP/TRUNCATE/无条件全表写/写系统表/读影子表/跨项目读写） |
| 5 | prepare | 冻结 Preparation 单：指纹、资源范围、影响范围、数据分级、运行预算、回滚方案；**此阶段零外部副作用** |
| 6 | approve | 双槽审批 + 强制四眼；**自批与同人占双槽由数据库约束拦截，不靠代码 if** |
| 7 | commit | 重算指纹比对 → 签 Ed25519 单次 Capability → **同一事务**内创建 ExecutionRun 与 Outbox 记录 |
| 8 | 执行 | Outbox 消费者 claim → 验签 → 防重放消费 → 真实读写影子表 → 回写指标；执行器可替换 |
| 9 | 运行时监督 | 行数/字节/时长/输出放大比/拒绝率五类预算，越线 warn 或 kill |
| 10 | 质量契约 | 字段级规则（非空/正数/邮箱格式/枚举）→ 不合规行分流到错误表并打 `error_code`，不污染正式表 |
| 11 | 原子发布 | 质量通过后事务内影子表 `RENAME` 换名发布 |
| 12 | 证据账本 | 关键事件哈希链追加 + 周期检查点签名；篡改任意一条，链校验失败 |
| 13 | 回滚 | 受管清理：撤销影子表、标记 run 为 rolled_back、写账本 |
| 14 | 测试 | 每条硬约束 **一个自动化用例**，用例名与约束编号一一对应 |

### 1.2 明确不做 / 降级模拟（**必须在 README 与答辩中主动声明**）

| 文档原文要求 | 本项目实现 | 答辩口径 |
| --- | --- | --- |
| SeaTunnel Zeta 真实集群 | 可替换执行器：`CsvToSqliteExecutor` 真实搬运数据 | "数据面引擎是可替换插槽，我实现的是协议与治理；换成 SeaTunnel 只需实现同一个 execute 接口" |
| Doris `REPLACE TABLE ... swap=true` | SQLite 事务内影子表 RENAME | 讲清"为什么必须原子换名而不是边写边读" |
| 5 类异构数据库全接 | SQLite + MySQL + CSV | 探查与执行逻辑同构，适配器接口一致 |
| Celery + Redis + PostgreSQL | Outbox + 同进程/Celery 双模式消费者，SQLite 存储 | "协议不变，生产把消费者换成 Celery 即可" —— **协议与 broker 解耦本身就是设计点** |
| LangGraph 框架 | 自研 DB-backed checkpoint 状态机（表 `workflow_checkpoints`） | "按 LangGraph 的 checkpoint 思路自己实现，不引框架" —— 反而避开"你读过框架源码吗" |
| 多租户 | 单项目多角色 | 租户隔离不是本项目的考点 |
| Benchmark 可视化大盘 | pytest 驱动的评测脚本，输出拦截率/准确率 JSON | 有数字，不做图表 |
| 安全进化闭环 | 不做，仅在文档中列为 next step | 诚实标注未做 |

> **纪律**：模拟部分不得在任何截图、报告、简历中用真实引擎的措辞描述。凡是模拟，就写"模拟执行器"。

---

## 2. 技术栈

沿用你已跑通的栈，避免新技术带来的额外调试成本：

- Python 3.12 / FastAPI / SQLAlchemy 2.x / Pydantic v2 / pytest
- 存储：SQLite（`data/guardrail.db`），开启外键；约束用 `CHECK` / `UNIQUE` / 触发器
- 签名：`cryptography`（Ed25519），令牌 JWK 式紧凑编码
- 前端：Jinja2 + 原生 JS（三页），不引前端框架
- LLM：可选适配层（OpenAI 兼容接口），默认关闭。关闭时用 `TemplatePlanner` 保证全链路可跑

---

## 3. 架构

```
┌──────────────────── 控制面 (FastAPI) ────────────────────┐
│  认证/角色 → 连接与探查 → Studio(需求→Plan→门禁)          │
│           → prepare → approve → commit                    │
│                        │                                  │
│         ┌──────────────┴───────────────┐                  │
│         │ PDP 风险决策  Capability Issuer│                  │
│         │ Evidence Ledger  Replay Guard │                  │
│         └──────────────┬───────────────┘                  │
│                        │ 同一事务                          │
│                  execution_runs + outbox_events            │
└────────────────────────┼──────────────────────────────────┘
                         │ claim（幂等）
┌────────────────────────▼──────────────────────────────────┐
│  Outbox Consumer → 验签 → 消费令牌 → Executor（可替换）     │
│     影子表写入 / 错误表分流 / 指标回传                     │
│                    ↑ RuntimeSupervision 监视               │
└───────────────────────────────────────────────────────────┘
```

### 3.1 三层状态隔离（面试必答点）

| 层 | 载体 | 生命周期 | 丢失后果 |
| --- | --- | --- | --- |
| 对话状态 Conversation | `agent_runs.prompt` + `chat_messages` | 会话级 | 只丢上下文，不影响执行 |
| 工作流状态 Workflow | `workflow_checkpoints`（DB） | 跨请求、跨人审等待（可能数小时） | 人审中途进程重启也能续跑 |
| 执行状态 Execution | `execution_runs` | 任务级，含引擎作业号与指标 | 是唯一有外部副作用的层 |

**为什么不能共用一个状态**：人审是异步长尾（审批人可能第二天才点），必须持久化；而执行状态一旦有副作用就不能靠"重放对话"恢复，只能靠 run 记录对账。

---

## 4. 数据模型

SQLite DDL 摘要。**加粗处是硬约束，全部要有对应用例。**

```sql
-- 身份与角色 --------------------------------------------------------------
users(id PK, username UNIQUE, password_hash, display_name, status, created_at)
projects(id PK, name, code UNIQUE, description)
project_memberships(id PK, project_id FK, user_id FK, role, UNIQUE(project_id,user_id))
project_role_grants(
  id PK, project_id FK, user_id FK, role_slot,
  **UNIQUE(project_id, user_id, role_slot)**          -- 一个槽可多人，但同一人同一槽只登记一次
)

-- 数据源与探查 ------------------------------------------------------------
connections(id PK, project_id FK, name, kind, dsn, secret_ref, status,
            **UNIQUE(project_id, name)**)
datasource_profiles(id PK, connection_id FK, schema_json, profile_digest,
                    sampled_at, created_at)

-- Pipeline 与不可变版本 ---------------------------------------------------
pipelines(id PK, project_id FK, name, code, status, created_at, updated_at,
          **UNIQUE(project_id, code)**)
pipeline_versions(
  id PK, pipeline_id FK, version_number, etl_plan_json, hocon_text,
  artifact_digest, is_immutable, created_by, created_at,
  **UNIQUE(pipeline_id, version_number)**
)
-- 不可变用触发器强制：is_immutable=1 后禁止 UPDATE etl_plan_json / hocon_text / artifact_digest

-- 工作流 ------------------------------------------------------------------
agent_runs(id PK, version_id FK, thread_id, prompt, status, step_count, created_at, updated_at)
workflow_checkpoints(id PK, thread_id, step_no, state_json, created_at,
                     **UNIQUE(thread_id, step_no)**)

-- 准备与审批 --------------------------------------------------------------
preparations(
  id PK, version_id FK, fingerprint, resource_scope_json, budget_json,
  rollback_plan_json, data_class, risk_level, submitter_id, status, created_at
)
approval_requests(id PK, preparation_id FK, required_role, status,
                  **UNIQUE(preparation_id, required_role)**)   -- 每个准备单每槽仅一条
approval_decisions(
  id PK, approval_request_id FK, approver_id, submitter_id, decision, comment, decided_at,
  **UNIQUE(approval_request_id)**,                             -- 一槽只能被决策一次
  **CHECK(approver_id <> submitter_id)**                        -- 自批在 DB 层被拦
)

-- 能力令牌 ----------------------------------------------------------------
capability_tokens(
  id PK, jti UNIQUE, execution_run_id FK, tool, subject_id, env,
  artifact_digest, issued_at, expires_at, consumed_at, sig
)

-- 执行 --------------------------------------------------------------------
execution_runs(
  id PK, version_id FK, preparation_id FK, capability_jti, status,
  engine_job_id, input_records, output_records, error_records, bytes_processed,
  started_at, finished_at, created_at, updated_at
)
outbox_events(
  id PK, aggregate_type, aggregate_id, event_type, payload_json,
  status, attempts, claimed_by, claimed_at, created_at,
  **UNIQUE(aggregate_id, event_type)**,                        -- 天然幂等：一 run 一事件
  CHECK(status IN ('pending','claimed','done','dead'))
)
runtime_supervision_snapshots(id PK, execution_run_id FK, metrics_json, decision,
                               action_taken, created_at)
quality_error_rows(id PK, execution_run_id FK, source_row_no, error_code, raw_json, created_at)

-- 证据账本（只追加） ------------------------------------------------------
audit_events(
  id PK, project_id, actor_id, event_type, resource_type, resource_id,
  payload_digest, prev_event_hash, event_hash, created_at
)
ledger_checkpoints(id PK, last_event_id, last_event_hash, signature, signed_at)
```

---

## 5. 核心协议

### 5.1 `prepare → approve → commit`

**Prepare（零副作用）**

1. 读 `pipeline_versions` 的最新不可变版本
2. 读绑定连接的 `datasource_profiles.profile_digest`
3. 计算指纹：

```
fingerprint = sha256(
  artifact_digest ‖ profile_digest ‖ normalize(resource_scope) ‖
  normalize(budget) ‖ executor_version ‖ env
)
```

4. 冻结 `preparations` 单：资源范围（哪些表/库）、影响范围（预计写入行数量级）、数据分级、
   运行预算、回滚方案
5. 调 PDP 出风险等级 `P0..P3`，决定需要哪些审批槽

**Approve（独立、具名、不可自批）**

- 每个 `approval_requests` 由不同具名用户决策
- 自批、同人占双槽均由 DB 约束拦截（见 §4 加粗项）
- 任一槽 reject → Preparation 结束，不进入 commit

**Commit（同一事务内完成三件事）**

1. **重算指纹并与 `preparations.fingerprint` 比对**（防 TOCTOU：审批的是 A 版本，执行时被换成 B）
2. 校验该准备单所有必需槽位都已 approve
3. 签 Capability，写 `execution_runs` + `outbox_events`

> 事务边界：这三件事必须在 `BEGIN … COMMIT` 内，否则会出现"令牌签了但 run 没建"的悬空授权。

### 5.2 Capability 令牌

```
payload = {jti, tool, subject_id, env, artifact_digest, run_id, iat, exp}
sig     = Ed25519.sign(canonical_json(payload))
exp     = iat + 300s
```

- 绑定四要素：**工具 + 主体 + 环境 + 制品指纹**。任一不符 → 验签拒绝
- 有效期 5 分钟；`Replay Guard` 保证只能消费一次

**防重放的正确写法（面试考点）**：

```sql
-- 错误：先查后写 → 并发竞态
-- 正确：用受影响行数当判据
UPDATE capability_tokens
   SET consumed_at = :now
 WHERE jti = :jti AND consumed_at IS NULL AND expires_at > :now;
-- affected_rows == 1 才放行；== 0 一律拒绝
```

### 5.3 Outbox 与幂等

- 业务事实与待投递命令同事务落库 → 消除"DB 成功但消息丢"/"消息发了事务回滚"
- 消费者 claim：

```sql
UPDATE outbox_events
   SET status='claimed', claimed_by=:worker, claimed_at=:now, attempts=attempts+1
 WHERE id = (SELECT id FROM outbox_events
              WHERE status='pending' ORDER BY id LIMIT 1)
   AND status='pending';
```

- `attempts > 3` → `status='dead'`，进入人工处置（**不静默丢弃**）
- 执行侧幂等键 = `(execution_run_id)`：重复消费直接返回既有结果，不重复搬运

### 5.4 运行时监督

五类预算，冻结在 `preparations.budget_json`：

| 指标 | 触发 warn | 触发 kill | 检查点 |
| --- | --- | --- | --- |
| 读取行数 | 80% | 100% | 每 N 行 |
| 写入字节 | 80% | 100% | 每 N 行 |
| 执行时长 | 80% | 100% | 每批次 |
| 输出放大比 | > 1.5 | > 3.0 | 每批次 |
| 错误拒绝率 | > 5% | > 20% | 每批次 |

越线动作：写 `runtime_supervision_snapshots` → 标记 run 状态 → kill 时撤销影子表。

### 5.5 质量契约与原子发布

1. 有效行写 `shadow_{target}`；不合规行写 `quality_error_rows` 并打 `error_code`
   （`NULL_REQUIRED` / `NEGATIVE_AMOUNT` / `BAD_EMAIL` / `ENUM_MISS` / `TYPE_MISMATCH`）
2. 拒绝率在预算内 → 事务内：`ALTER TABLE shadow_x RENAME TO x_old; ALTER TABLE x RENAME TO shadow_x;`
3. 发布后写审计事件

### 5.6 证据账本

```
event_hash = sha256(prev_event_hash ‖ canonical_json(event_payload))
```

- 每 100 条或每次 run 结束，写一条 `ledger_checkpoints`（对 `last_event_hash` 做 Ed25519 签名）
- 校验器 `verify_ledger()`：从头重算链 → 与检查点签名比对
- **为什么检查点也要签**：否则攻击者可以整链重算让篡改自洽

---

## 6. 状态机

```
Pipeline:      draft → generating → review → prepared → approved → committed
                                                                     ↓
ExecutionRun:  pending → claimed → running → succeeded
                                      ├→ failed
                                      ├→ killed（监督越线）
                                      ├→ cancelled（操作员）
                                      └→ rolled_back（受管回滚）

Preparation:   frozen → awaiting_approval → approved → committed
                              └→ rejected
```

约束：一个 Pipeline 同时只允许一个处于 `prepared..committed` 的准备单（服务端 + 唯一索引双保险）。

---

## 7. 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 组件就绪状态 |
| POST | `/api/system/init` | 初始化管理员 |
| POST | `/api/system/login` | 登录 |
| GET | `/api/projects` | 项目列表 |
| POST | `/api/projects/{pid}/connections` | 登记数据源 |
| POST | `/api/connections/{cid}/tests` | 连通性测试 |
| POST | `/api/connections/{cid}/profiles` | 只读探查 + 脱敏样本 |
| POST | `/api/projects/{pid}/pipelines` | 创建 Pipeline |
| POST | `/api/versions/{vid}/generation` | 需求 → EtlPlan 候选 |
| POST | `/api/versions/{vid}/gate` | 确定性门禁校验（返回 P0-P3 结论） |
| POST | `/api/versions/{vid}/freeze` | 冻结不可变版本 + 摘要 |
| GET | `/api/versions/{vid}/design` | 查 Plan / 配置 / DAG / 质量契约 |
| POST | `/api/versions/{vid}/prepare` | 生成 Preparation 单 |
| POST | `/api/approval-requests/{aid}/decisions` | 具名审批 approve/reject |
| POST | `/api/preparations/{pid}/commit` | 验指纹 → 签令牌 → 入队 |
| GET | `/api/execution-runs/{rid}` | 状态、指标、质量报告 |
| POST | `/api/execution-runs/{rid}/cancel` | 取消 |
| POST | `/api/execution-runs/{rid}/rollback` | 受管回滚 |
| GET | `/api/projects/{pid}/ledger` | 审计事件列表 |
| GET | `/api/projects/{pid}/ledger/verify` | 链完整性校验 |
| WS | `/ws/projects/{pid}/stream` | 实时 run 状态、指标、日志 |

---

## 8. 测试矩阵（交付硬证据）

每个用例名 = 约束编号。`pytest -q` 全绿才算完成（**但全绿不构成效果证据，见 §9**）。

| 编号 | 约束 | 用例 | 验证方式 |
| --- | --- | --- | --- |
| C1 | 禁止自批 | `test_c1_self_approval_rejected` | API 提交被拒；**且绕过 API 直插 SQL 也报 CHECK 失败** |
| C2 | 禁止同人占双槽 | `test_c2_dual_slot_same_user` | UNIQUE 冲突 |
| C3 | 令牌过期/复用 | `test_c3_replay_rejected` | 第二次消费 affected_rows=0 |
| C4 | 制品替换 TOCTOU | `test_c4_fingerprint_mismatch` | commit 前改 version → 409 |
| C5 | 未审批不得 commit | `test_c5_commit_requires_all_slots` | 少一槽 → 拒绝且不签令牌 |
| C6 | Outbox 双写一致性 | `test_c6_outbox_crash_recovery` | 提交后强杀消费前进程 → 重启后仍执行 |
| C7 | 越预算 kill | `test_c7_budget_kill` | 构造超限数据 → run=killed 且影子表被撤 |
| C8 | 质量分流 | `test_c8_error_rows_not_published` | 错误行只在错误表，正式表无污染 |
| C9 | 未发布不可见 | `test_c9_shadow_not_visible` | 发布前查目标表仍是旧数据 |
| C10 | 账本防篡改 | `test_c10_ledger_tamper_detected` | 改一条 payload → verify 失败 |
| C11 | 不可变版本 | `test_c11_immutable_version_locked` | UPDATE 被触发器拒绝 |
| C12 | 越权访问 | `test_c12_role_matrix` | 五角色 × 12 接口权限矩阵 |

---

## 9. 验收与证据要求（按团队口径）

1. **测试全绿 ≠ 交付证据**。必须附：每个用例的一条实际输出截图/日志，含 run_id 与时间戳
2. **数字先核源**：任何"拦截率""成功率"数字，附分子分母与数据集来源
3. **失败不静默**：`outbox_events.status='dead'`、门禁 P0 拦截、监督 kill 均要有计数器并在页面上可见
4. **模拟边界清单**单独成节（§1.2），与真实能力并列呈现，不含糊
5. 端到端演示脚本 `scripts/demo.py`：一条成功链路 + 一条被 kill 后回滚链路，可重复执行

---

## 10. 四周节奏

| 周 | 交付 | 出门条件 |
| --- | --- | --- |
| W1 | 表结构 + 认证 + 角色 + 连接/探查 + 三层状态机骨架 | 能登录、能探查、错 schema 有明确报错 |
| W2 | **prepare / approve / commit / 令牌 / Replay Guard** | C1-C5、C11 用例全绿（答辩主战场） |
| W3 | Outbox + 消费者 + 执行器 + 监督 + 质量分流 + 原子发布 + 回滚 | C6-C9 全绿，端到端跑通 |
| W4 | 证据账本 + 三个前端页 + `demo.py` + README 模拟边界 + 简历条目 | C10、C12 全绿，演示可复现 |

**风险**：W2 是唯一不能压缩的一周。如果时间不够，砍 W4 的前端（用 curl + 截图替代），不砍 W2 的约束。

---

## 11. 这个项目能接住的面试题

1. 令牌为什么必须绑制品指纹？→ 防 TOCTOU：审批 A 执行 B
2. 为什么用 Outbox 不直接抛任务？→ 双写不一致的两种失败态
3. 影子表 + 原子换名解决什么？→ 避免半量数据被下游读到；换名是元数据操作
4. 职责分离怎么保证不是靠自觉？→ UNIQUE + CHECK，绕 API 直连库也拦得住
5. 中断恢复为什么不能用内存状态？→ 人审是长尾异步，进程必然重启
6. Replay Guard 为什么不能用"先查再写"？→ 并发竞态，必须用受影响行数
7. 哈希链怎么防篡改？→ 改一条后续全对不上；检查点签名防整链重算
8. 预算 kill 之后数据怎么办？→ 撤销影子表 + 标记 rolled_back + 写账本，正式表从未被碰
9. 哪些是模拟的？→ 主动答，并说明"协议为真、引擎可替换"

---

## 12. 简历条目（草稿）

> **ETL-Agent 数据集成控制面（个人项目）** · Python / FastAPI / SQLAlchemy / Ed25519
> - 设计并实现 `prepare → approve → commit` 三阶段执行协议，用数据库 `UNIQUE`/`CHECK` 约束强制四眼审批与职责分离，绕过 API 直连数据库亦无法自批
> - 以 Ed25519 单次 Capability（绑定工具/主体/环境/制品 SHA256，5 分钟有效）实现受管执行，Replay Guard 用受影响行数判据消除并发重放竞态
> - 事务 Outbox + 幂等消费消除双写不一致；影子表 + 质量契约分流 + 原子换名发布，运行时五维预算越线自动 kill 并回滚
> - 关键事件哈希链 + 检查点签名实现防篡改审计，篡改任意一条可被检出

---

## 13. 待你定的三个决策

| 决策 | 选项 A | 选项 B | 我的倾向 |
| --- | --- | --- | --- |
| LLM 接入 | 接真实模型生成 EtlPlan | 模板 + 规则，纯确定性 | **B 为默认，A 作为可选适配层**。LLM 在这里不是考点，反而会让人问"你生成对了几个" |
| 工作流 | 自研 DB checkpoint | 引 LangGraph | **A**。可控、可讲、无版本风险 |
| 是否真接 MySQL | 接（需本地起服务） | 只 SQLite + CSV | 先 B，W3 有余力再补 A（适配器接口一开始就留好） |

---

*本规格书为开发前基线。落地过程中任何偏离，改这里而不是只在代码里偷偷改。*
