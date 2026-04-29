# 系统 vs Top 论文方法 — 全维度对比报告

> **日期**: 2026-03-19
> **当前 EX**: 66% (100题) / 61.8% (500题)
> **目标**: Top 10 ≥76% EX
> **覆盖**: 600/1534 已测试 (39.1%)

---

## 一、架构总览

```
                Agentar (#3, 81.67%)           Our System (66%)            Contextual (#15, 75.63%)       CHASE-SQL (#10, 76.02%)
                ═══════════════════           ════════════════            ═══════════════════════       ══════════════════════

Question →      NLU (Gemini-Flash)            prepare_run.py              Prompt template              Keyword extraction (o4-mini)
                       ↓                             ↓                          ↓                            ↓
Schema →        Light Schema (全量)           Bidirectional SL             MSchema (全量)              Qdrant列检索 + 列筛选
                       ↓                       (TF + CF merge)                 ↓                            ↓
Few-shot →      Skeleton ICL ×15             Skeleton ICL ×15            Random 1-shot ×32轮           动态生成示例
                       ↓                             ↓                          ↓                            ↓
生成 →          3候选 (多模型多温度)          ⚠️ 1候选 (Sonnet S0)        1024候选 (32×32采样)         1候选 + fallback
                Gemini T=1.8/0.5              实际全用 force S0            Qwen2.5-Coder-32B             o4-mini → Claude fallback
                + GPT-5 T=1.0                                             T=1.0
                       ↓                             ↓                          ↓                            ↓
选择 →          Pairwise Tournament           ❌ 无 (单候选)              logprob + Reward Model        直接输出 / 2轮修复
                (Qwen-32B 两两比较)                                       0.4×logprob + 0.6×reward
                       ↓                                                        ↓                            ↓
修复 →          SQL Fixer + Revisor           ❌ 无                       ❌ 无 (靠数量覆盖)            2轮迭代修复
                (Gemini, T=0.2/1.0)
```

---

## 二、逐维度详细对比

### 1. Schema 表示

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| 格式 | Light Schema + Markdown 表格 | **Light Schema + Markdown 表格** ✅ | MSchema (半结构化) | DDL + 筛选列 |
| 每列样本值 | 3 random DISTINCT | **3 random DISTINCT** ✅ | 5 DISTINCT | 无 |
| 列描述 | column_meaning | **column_meaning** ✅ | 内嵌描述 | 无 |
| 主键/外键 | ✅ | **✅** | ✅ | ✅ |
| **差距** | — | **无差距** | — | — |

**实现文件**: `light_schema.py`, `artifacts/llm_schema_defs/`

**Prompt 中实际效果**:
```
| column_name | column_type | description               | sample_values                        |
|-------------|-------------|---------------------------|--------------------------------------|
| circuitId   | INTEGER     | Unique ID of the circuit  | 49, 33, 21                           |
| name        | TEXT        | Full name of circuit      | Circuit of the Americas, Korean ...   |
| country     | TEXT        | Country of circuit        | Monaco, Argentina, Morocco            |
```

---

### 2. Schema Linking

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| 方式 | ❌ 无（全量 schema） | **双向 TF+CF merge** ✅ | ❌ 无（全量） | Qdrant 向量检索列 |
| Table Recall | N/A (全量) | **100%** | N/A (全量) | 未知 |
| Table Precision | N/A | **36.7%** | N/A | 未知 |
| Table F1 | N/A | **51.6%** | N/A | 未知 |
| **差距** | — | **我们独有优势** | — | — |

**实现文件**: `schema_linker.py`, `scripts/run_pipeline.py` (phase_schema_link, phase_merge_sl)

**方法**: 双向 Schema Linking (TF=Table-First + CF=Column-First) → Union merge，最大化 Recall。
- Table-First: 从问题实体出发识别表
- Column-First: 从列引用和公式出发识别表
- 合并: Union 取并集，确保不遗漏

---

### 3. Few-shot / ICL

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| 来源 | BIRD train 9,428题 | **BIRD train 9,428题** ✅ | BIRD train 7,428题 | 动态生成 |
| 数量 | 15-shot | **15-shot** ✅ | 1-shot × 32轮 | 变化 |
| 检索方式 | ChromaDB embedding | **BGE-M3 dense + 特征重排** ✅ | Random | Qdrant 向量 |
| Skeleton 提取 | LLM-based (Gemini-Flash) | **规则-based** (更快更稳定) | N/A | N/A |
| 多样性控制 | 未知 | **Max 2/signature, Max 2/db** ✅ | 每轮随机 | N/A |
| **差距** | — | **功能等价，实现不同** | — | — |

**实现文件**: `skeleton_icl.py`, `artifacts/skeleton_icl/bird_train_dense.pkl` (43MB, 9428条预计算嵌入)

**检索流程**:
1. 问题 → BGE-M3 编码 → 余弦相似度 → Top-40 候选池
2. 特征重排: `score = base + 0.18×feature_overlap - 0.10×feature_penalty + 0.22×jaccard`
3. 去重: 同 SQL signature ≤2, 同 db_id ≤2
4. 输出 Top-15 示例，注入 `{{SKELETON_ICL_EXAMPLES}}`

---

### 4. Value Grounding（字面量匹配）

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| 索引方式 | ChromaDB 向量嵌入 | **JSONL 全扫描 + 分层索引** ✅ | 5 DISTINCT 值内嵌 schema | Qdrant 向量 |
| 检索方式 | 向量相似度 | **Token overlap + 子串 + 频率** ✅ | 静态内嵌 | 向量相似度 |
| Zero-padded 数字 | 未知 | **✅ 专门处理 (live DB lookup)** | ❌ | ❌ |
| 运行时注入 | ✅ | **✅ `{{VALUE_GROUNDING_HINT}}`** | 静态 (schema内) | ✅ |
| **差距** | — | **功能等价，Zero-padded 更强** | — | — |

**实现文件**: `db_value_index.py`, `scripts/build_db_value_index.py`, `artifacts/db_profiles/*/value_index.jsonl`

**索引策略 (两层)**:
- **Full scan** (distinct ≤ 500): enum_like, name_like, low-cardinality TEXT → 全量扫描
- **Top-values** (distinct > 500): 仅取 profile 中 Top 10 频繁值

**Prompt 中实际效果**:
```
## Exact Values Found in Database
The following exact values exist in the database — use these exact spellings/formats in WHERE clauses:

- `account.frequency`: 'POPLATEK PO OBRATU'
- `district.A3`: 'east Bohemia'
```

**已验证成功案例**:
- QID 86: `CharterNum = '0040'` (zero-padded 格式检测)
- QID 66: `'Directly funded'` (精确大小写匹配)
- QID 376: 关键词精确匹配

---

### 5. ⭐ 候选生成（最大差距）

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| **候选数** | **3** | **1** ❌ | **1,024** | **1+fallback** |
| 模型家族 | Gemini×2 + GPT-5 | **仅 Claude Sonnet** ❌ | Qwen-32B (本地) | o4-mini + Claude |
| 温度多样性 | T=0.5, 1.0, 1.8 | **默认温度** ❌ | T=1.0 (靠 prompt 变体) | 默认 |
| Extended Thinking | Gemini thinking=128 | **❌ 未启用** | ❌ | Claude ET (10K budget) |
| Prompt 多样性 | 单 prompt + 多温度 | 有 decomposed 但未用 | 32 prompt 变体 | 标准 + CoT |
| **差距** | — | **🔴 最大差距** | — | — |

**关键发现**: S1/S2 多候选路由已实现（`runtime_config.json` 中配置了 S1=[Opus, Sonnet], S2=[Opus, Sonnet]），但**所有基准测试均使用 `--force-route S0`**（单 Sonnet 候选），多候选能力从未实际启用。

**已实现但未启用的配置**:
```json
"route_strategy": {
    "S0": [["direct", "sonnet"]],           ← 所有跑分都用这个
    "S1": [["candidate_A", "opus"], ["candidate_B", "sonnet"]],  ← 从未启用
    "S2": [["candidate_A", "opus"], ["candidate_B", "sonnet"]]   ← 从未启用
}
```

**候选数与 Oracle Accuracy 的关系** (来自论文研究):

| 候选数 | 估算 Oracle Acc | 实际可达 (有效选择器) | 系统 |
|--------|----------------|---------------------|------|
| 1 | ~65% | ~65% | **我们 (当前)** |
| 2 | ~72% | ~68% | **我们 (S1/S2 启用后)** |
| 3 | ~78% | ~74% | Agentar |
| 21 | ~88% | ~76% | CHASE-SQL |
| 1,024 | ~95% | ~75.6% | Contextual |

---

### 6. ⭐ 选择机制（第二大差距）

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| 方式 | **Pairwise Tournament** | **多层级联 (L1/L2/条件打分)** | **logprob + Reward Model** | 直接输出 / 修复 |
| 判断模型 | Qwen-32B (两两比较) | 规则匹配 (无模型) | 训练的 RM | N/A |
| 比较输入 | SQL + 执行结果 + 语义 | 可执行性 + 结果一致 + 条件打分 | 概率 + 质量评分 | N/A |
| 比赛数 | C(3,2) = 3 场 | N/A (规则级联) | N/A (打分排序) | N/A |
| **差距** | — | **⚠️ 有实现但单候选下无意义** | — | — |

**已实现的选择逻辑** (`scripts/run_pipeline.py` phase_select):
- **Layer 1**: 可执行性过滤 — A 可执行 B 不可 → 选 A
- **Layer 2**: 结果比较 — 两个都可执行且结果一致 → 选 B (Sonnet, 成本低)
- **Layer 3**: 条件打分 — ORDER BY/ROUND/DISTINCT 条件匹配 → 选分高者

**问题**: 当前全部 S0 单候选，选择机制完全不起作用。

---

### 7. SQL 修复/精炼

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| SQL Fixer | ✅ Gemini T=0.2 (语法纠错) | **❌ 未实现** | ❌ (靠数量) | ✅ 2轮迭代修复 |
| SQL Revisor | ✅ Gemini T=1.0 (语义优化) | **❌ 未实现** | ❌ | ❌ |
| 触发条件 | 每个候选都过 Fixer | N/A | N/A | 执行失败时触发 |
| **差距** | — | **🟡 中等差距** | — | — |

**实现文件**: 无。Pipeline 阶段为 prepare → retrieve → schema-link → build → generate → select，没有 fix/revise 阶段。

---

### 8. 数据库特异性知识（我们的独有优势）

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| DB Profile | ❌ 无 | **✅ 11库全量 profile** | ❌ | ❌ |
| Auto DB Hints | ❌ 无 | **✅ 4类自动 hints** | ❌ | ❌ |
| Error Memory | ❌ 无 | **✅ 72条错误模式** | ❌ | ❌ |
| 手写 Hints | ❌ 无 | **✅ card_games 域知识** | ❌ | ❌ |
| **差距** | — | **🟢 我们独有** | — | — |

**Auto DB Hints 4 类** (`auto_db_hints.py`):
1. Non-Unique Keys → 提示 DISTINCT 需求
2. 1:N JOIN Risks → 避免 COUNT inflation
3. Stored Aggregates → 禁止重复聚合
4. High-NULL Columns → 提示加 IS NOT NULL

**Error Memory** (`error_memory.py`):
- 72 条从历史失败中提取的模式
- 按 (db_id, error_type, tables) 聚合
- 矛盾抑制: missing_distinct ↔ spurious_distinct 同时出现时抑制
- 特异性加权: 具体模式 10× boost over 泛型

**已知问题**:
- hints 过多过泛 (formula_1 有 19 条 JOIN risk)
- stored_aggregate 误判 (如 `drivers.number` 是车号不是聚合值)
- 只提示 COUNT 去重，未提示 SELECT DISTINCT

---

### 9. Evidence / 外部知识

| 维度 | Agentar | **我们** | Contextual | CHASE-SQL |
|------|---------|---------|-----------|----------|
| 方式 | 直接注入 prompt | **直接注入 prompt** ✅ | 直接注入 | 直接注入 |
| **差距** | — | **无差距** | — | — |

---

## 三、差距优先级排序与预期收益

| 优先级 | 差距项 | 预期收益 | 实现难度 | 当前状态 | 说明 |
|--------|--------|---------|---------|---------|------|
| **P0** | **启用 S1/S2 多候选** | **+3~5%** | **极低** | 代码已有，去掉 force S0 即可 | 从 1 候选 → 2 候选 (Opus+Sonnet) |
| **P1** | 增加第三模型家族 (Gemini/GPT) | +2~3% | 中 | 需接 API | 同家族多样性有限 |
| **P2** | SQL Fixer 阶段 | +1~2% | 中 | 需新增 pipeline 阶段 | 执行失败时尝试修复 |
| **P3** | Extended Thinking | +0.5~1% | 低 | 配置变更 | Claude Sonnet 支持 ET |
| **P4** | Pairwise Tournament 升级 | +1~2% | 中 | 替换 L1/L2 选择 | 用 LLM 做两两语义比较 |
| **P5** | DB Hints 精炼 | +1~2% | 低 | 优化现有 | 压缩数量 + 补充 SELECT DISTINCT |
| | **累计预期** | **+8.5~15%** | | | **目标 74~81%** |

---

## 四、核心结论

### ✅ 已对齐的能力 (与 Top 系统无差距)

| 能力 | 状态 |
|------|------|
| Light Schema + 3 samples/col | ✅ 完全对齐 Agentar |
| Skeleton ICL 15-shot (BIRD train) | ✅ 完全对齐 Agentar |
| Cell Value Grounding (运行时检索+注入) | ✅ 功能等价 |
| Bidirectional Schema Linking | ✅ 独有优势 (Agentar 无) |
| Evidence 利用 | ✅ 完全对齐 |

### ❌ 未利用的已有能力 (最快收益)

| 能力 | 状态 | 行动 |
|------|------|------|
| S1/S2 多候选路由 | 已实现，未启用 | **去掉 `--force-route S0`** |
| Decomposed prompt (两步推理) | 已实现，未使用 | 随 S1/S2 自动启用 |
| L1/L2 选择逻辑 | 已实现，无输入 | 随多候选自动生效 |

### ❌ 真正缺失的能力

| 能力 | 影响 |
|------|------|
| 第三模型家族 (Gemini/GPT) | 减少同家族错误相关性 |
| SQL Fixer/Revisor | 修复可执行但语义错误的 SQL |
| Extended Thinking | 复杂推理 (Challenging 难度) |
| Pairwise Tournament (LLM-based) | 更精确的候选选择 |

### 🔑 一句话总结

> **基础设施已全面对齐 Top 系统，但最大的收益 (+3~5%) 来自一个配置变更: 去掉 `--force-route S0`，启用已实现的多候选机制。**

---

## 五、当前测试数据概览

### 5.1 覆盖率

| 指标 | 数值 |
|------|------|
| 总题目数 | 1,534 |
| 已测试 | 600 (39.1%) |
| 未测试 | 934 (60.9%) |
| 总运行次数 | 34 (有效) |

### 5.2 数据库覆盖

| 数据库 | 总题数 | 已测试 | 覆盖率 | 最新 EX% |
|--------|--------|--------|--------|----------|
| codebase_community | 186 | 154 | 82.8% | 100% (3/3 新题) |
| debit_card_specializing | 64 | 45 | 70.3% | 50% (1/2 新题) |
| card_games | 191 | 131 | 68.6% | 33.3% (2/6 新题) |
| california_schools | 89 | 59 | 66.3% | 20% (1/5 新题) |
| financial | 106 | 66 | 62.3% | 0% (0/4 新题) |
| european_football_2 | 129 | 71 | 55.0% | 50% (3/6 新题) |
| formula_1 | 174 | 17 | 9.8% | 52.9% (9/17 新题) |
| thrombosis_prediction | 163 | 16 | 9.8% | 81.2% (13/16 新题) |
| student_club | 158 | 15 | 9.5% | 86.7% (13/15 新题) |
| superhero | 129 | 12 | 9.3% | 91.7% (11/12 新题) |
| toxicology | 145 | 14 | 9.7% | 71.4% (10/14 新题) |

### 5.3 最佳成绩

| 测试类型 | 最佳成绩 | Run ID |
|----------|---------|--------|
| 100题基准 | 67/100 (67%) | run_20260317_234211 |
| 500题基准 | 309/500 (61.8%) | run_20260318_181735 |
| 新100题扩展 | 66/100 (66%) | run_20260319_225038 |

### 5.4 稳定性分析 (测试 3+ 次的题目)

| 类别 | 数量 | 占比 |
|------|------|------|
| 稳定正确 (100% 正确率) | 232 | 46.4% |
| 多数正确 (>50%) | 74 | 14.8% |
| 多数错误 (<50%) | 59 | 11.8% |
| 稳定错误 (0% 正确率) | 135 | 27.0% |

### 5.5 新100题错误分析

| 根因类别 | 数量 | 占比 | 可修复性 |
|---------|------|------|---------|
| missing_DISTINCT | 7 | 20.6% | 中 — formula_1 集中爆发 |
| wrong_logic (语义理解) | 6 | 17.6% | 难 — 需要深层推理 |
| wrong_column (列选择) | 4 | 11.8% | 中 — 列名歧义 |
| wrong_aggregation | 3 | 8.8% | 中 — SUM vs AVG vs COUNT |
| missing_select_col | 2 | 5.9% | 中 — 漏选列 |
| wrong_denominator | 2 | 5.9% | 中 — 分母范围选错 |
| case_sensitivity | 2 | 5.9% | ⚠️ Gold SQL 本身错误 |
| LIKE_pattern | 2 | 5.9% | 易 — 时间格式提示 |
| 其他 (JOIN/CAST等) | 6 | 17.6% | 各异 |

---

## 六、数据追踪

所有运行数据已记录在 SQLite 数据库中:

```
output/benchmark_tracker.db
```

**表结构**:
- `runs`: 每次运行的元数据 (run_id, timestamp, total, correct, ex_pct, run_type)
- `question_results`: 每题每次的详细结果 (pred_sql, gold_sql, exec_correct, fail_tag)
- `question_coverage`: 每题的汇总统计 (times_tested, times_correct, stability)

**视图**:
- `v_run_progression`: 按类型的 EX% 变化趋势
- `v_question_stability`: 题目稳定性分类
- `v_db_coverage`: 数据库覆盖率统计
