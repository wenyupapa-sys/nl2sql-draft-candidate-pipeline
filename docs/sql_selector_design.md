# SQL Candidate Selector 设计文档

> 基于 BIRD Top 5 Repos 的 Selector 机制研究，设计多模型生成 + 执行聚类 + LLM 仲裁的候选选择方案。

---

## 1. SOTA Selector 机制研究

### 1.1 两类 Selector

| 类型 | 目的 | 代表系统 |
|------|------|----------|
| **Schema Selector** | 筛选相关表/列，缩减 prompt | MAC-SQL, CHESS |
| **SQL Candidate Selector** | 从多个候选 SQL 中选最优 | Agentar, CHESS, XiYan-SQL, CHASE-SQL |

### 1.2 四种 SQL Candidate Selector 方案

#### Agentar — RL 训练的 Pairwise 锦标赛 (81.67% test)

- **生成**: 17 个候选（9 ICL + 8 Reasoning），3 种 prompt 风格 × 多温度 × 多模型
- **选择**:
  1. 按执行结果分组去重，每组取一个代表
  2. 所有代表两两比较（pairwise round-robin）
  3. 累计赢次最多的候选胜出
- **Selector prompt 核心**:
  ```
  Analyze each candidate's query and execution result for correctness,
  completeness, and relevance. Output: \boxed{candidate_number}
  ```
- **RL 训练**: GRPO, reward = 1 if correct / 0 otherwise, 8.5k 训练样本
- **消融实验**: 去掉 selector → -1.82pp

#### CHESS — Unit Test + 执行一致性 (61.5% dev)

- **生成**: 3 个候选（分治法 / Query Plan CoT / Direct）
- **选择**:
  1. LLM 根据问题生成自然语言断言（如 "结果应只有一行"）
  2. 对每个候选 SQL 评估是否通过每个测试，计分
  3. 得分相同时，选执行结果最多人相同的（majority cluster）
- **伪代码**:
  ```
  clusters = cluster_by_execution_result(candidates)
  tests = llm_generate_unit_tests(question, hint, clusters)
  scores[c] = count(passed_tests for c)
  winner = max(scores) → tiebreak by largest_cluster
  ```

#### XiYan-SQL — 微调 Selector 模型 (75.63% test)

- **生成**: 5 个 generator × 2 轮 schema = ~10 个候选
- **选择**:
  1. 按执行结果聚类 → 大组排前 → 组内按 generator 性能排序
  2. 微调的 Qwen2.5-Coder-7B 直接输出最优候选
- **训练**: 对比学习，正例 = 执行结果匹配 gold，负例 = 不匹配
- **效果**: 微调 selector 比 majority voting 高 +2.45pp（72.39% vs 69.94%）

#### CHASE-SQL — 微调 Pairwise 二分类 (73.01% dev)

- **选择**:
  1. 对所有候选对执行 pairwise 比较
  2. 同执行结果 → 直接加分
  3. 不同结果 → 微调的二分类模型判定（输入: question + 两个 SQL + union schema）
  4. 累计得分最高者胜出
- **微调**: Gemma 2 9B / Gemini-1.5-Flash
- **关键发现**: 未微调的 Claude-3.5 / Gemini-1.5-Pro 只有 ~58% 准确率 → 微调至关重要

### 1.3 共性模式

| 模式 | 说明 | 使用系统 |
|------|------|----------|
| **执行结果聚类** | 按 DB 执行结果分组，相同结果 = 语义等价 | 全部 |
| **Pairwise > Majority Vote** | 两两对比优于频率投票 | Agentar, CHASE-SQL |
| **微调 > Prompting** | selector 需要微调才有效 | XiYan, CHASE-SQL |
| **多样性生成** | 多 prompt 风格 × 多温度 × 多模型 | 全部 |
| **Tiebreak 用 majority** | 得分相同时回退到执行结果一致性 | CHESS, XiYan |
| **投票数传给 selector** | 告知 "3个模型同意 vs 1个" 提升选择准确率 | XiYan, CHASE-SQL |

---

## 2. 我们的方案设计

### 2.1 整体架构

```
┌─────────────────────────────────────────────────┐
│              Step 1: 多模型并行生成                │
│                                                   │
│   Kimi    × [Decomposed, Direct] → 2 SQL          │
│   Gemini  × [Decomposed, Direct] → 2 SQL          │
│   GLM     × [Decomposed, Direct] → 2 SQL          │
│   Codex   × [Decomposed, Direct] → 2 SQL          │
│   Qwen    × [Decomposed, Direct] → 2 SQL          │
│   = 10 候选                                        │
└──────────────────────┬──────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────┐
│            Step 2: 执行 + 聚类                     │
│                                                   │
│   execute_all(10 SQL on SQLite)                    │
│   → 丢弃语法错误的候选                               │
│   → group_by_execution_result()                    │
│   → Cluster A: 4票  |  B: 3票  |  C: 1票          │
└──────────────────────┬──────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────┐
│            Step 3: 决策                            │
│                                                   │
│   if 最大 cluster ≥ 半数 (≥5票):                    │
│     → 直接选该 cluster 代表 (高置信度)               │
│   elif 最大 cluster ≥ 3票:                          │
│     → 大概率选它, 可选跳过 selector                  │
│   else:                                            │
│     → Gemini 3.1 Pro (temp=0) listwise 仲裁        │
└─────────────────────────────────────────────────┘
```

### 2.2 Step 1: 多模型多策略生成

#### 模型选择

| 模型 | 特点 | 成本 |
|------|------|------|
| Kimi (moonshot) | 长上下文, 中文理解强 | 低 |
| Gemini 2.5 Flash | 速度快, 推理能力强 | 低 |
| GLM-4 | 中文理解, 平衡型 | 低 |
| Codex (GPT-5.4) | 代码/SQL 推理最强 | 中 |
| Qwen-Max | 阿里系, schema 理解好 | 低 |

#### Prompt 策略

**策略 A: Decomposed（分解模式）**
- 先输出 Planning JSON（evidence_mapping, target_tables, join_paths, filters...）
- 再生成 SQL
- 适合复杂多表 JOIN 题目

**策略 B: Direct（直接模式）**
- 简化版 prompt, 去掉 Planning 步骤
- 直接 CoT → SQL
- 适合简单题, 且增加候选多样性

两种策略使用相同的 schema + rules + evidence, 只是输出格式不同。

### 2.3 Step 2: 执行聚类

```python
def cluster_candidates(candidates: list[str], db_path: str) -> dict:
    """按执行结果将候选 SQL 分组"""
    clusters = {}  # result_hash → [candidate_ids]
    errors = []

    for i, sql in enumerate(candidates):
        try:
            result = execute_sql(sql, db_path, timeout=30)
            result_key = hash(str(result))  # 按结果内容哈希
            clusters.setdefault(result_key, {
                "result": result,
                "candidates": [],
                "models": []
            })
            clusters[result_key]["candidates"].append(i)
            clusters[result_key]["models"].append(candidate_models[i])
        except Exception as e:
            errors.append((i, str(e)))

    # 按 cluster 大小降序排列
    sorted_clusters = sorted(
        clusters.values(),
        key=lambda c: len(c["candidates"]),
        reverse=True
    )
    return sorted_clusters, errors
```

**聚类注意事项**:
- 结果对比用内容哈希, 不用 SQL 文本对比（语义等价但写法不同的 SQL 应归为同一组）
- 空结果集单独成组（可能是 WHERE 条件过严）
- 执行超时 (>30s) 视为错误, 丢弃

### 2.4 Step 3: 决策逻辑

#### 快速路径: Majority 直出

```python
total_valid = sum(len(c["candidates"]) for c in sorted_clusters)

if len(sorted_clusters[0]["candidates"]) >= total_valid * 0.5:
    # 超过半数模型同意 → 高置信度直出
    return sorted_clusters[0]["candidates"][0]  # 取代表
```

预计 60-70% 的题目走快速路径, 无需额外 selector 调用。

#### 慢速路径: Gemini Pro Listwise 仲裁

当无明显多数时, 调用 Gemini 3.1 Pro 做 listwise 选择:

```
你是一个 SQL 评估专家。给定数据库 schema、问题、evidence 和多个候选 SQL,
请选出最正确的一个。

## 数据库 Schema
{schema}

## 问题
{question}

## Evidence
{evidence}

## 候选 SQL 及执行结果

### 候选 1 (3 个模型同意: Kimi, Gemini, Qwen)
```sql
SELECT ...
```
执行结果 (前5行):
| col1 | col2 |
|------|------|
| ...  | ...  |

### 候选 2 (2 个模型同意: GLM, Codex)
```sql
SELECT ...
```
执行结果 (前5行):
| col1 | col2 |
|------|------|
| ...  | ...  |

### 候选 3 (1 个模型: Kimi-direct)
```sql
SELECT ...
```
执行结果 (前5行):
| col1 | col2 |
|------|------|
| ...  | ...  |

## 评估要求
1. 对比每个候选 SQL 的逻辑是否与问题匹配
2. 检查 JOIN 路径是否正确
3. 检查 WHERE 条件是否覆盖 evidence 中的所有约束
4. 检查聚合方式 (COUNT/SUM/AVG) 是否正确
5. 注意: 投票数多不一定对, 但投票数是参考信号

## 输出
先给出分析推理, 然后输出:
最终选择: 候选 {N}
```

#### 为什么用 Listwise 而非 Pairwise

| 方式 | 调用次数 (5 cluster 代表) | 适用场景 |
|------|--------------------------|----------|
| Pairwise | C(5,2) = 10 次 | 候选 >20, 需要精细区分 |
| Listwise | 1 次 | 候选 ≤10, 足够区分 |

我们去重后通常只有 3-5 个 cluster 代表, listwise 一次调用足矣。

### 2.5 成本估算

| 步骤 | 调用次数/题 | 模型 | 每题成本 |
|------|------------|------|----------|
| 生成 | 10 | 各模型 | ~$0.02-0.05 |
| 执行 | 10 | SQLite | 免费 |
| Selector | 0~1 | Gemini Pro | ~$0.01 (仅 30-40% 题触发) |
| **总计** | | | **~$0.03-0.06/题** |

全量 1534 题: ~$50-90

---

## 3. 与当前系统对比

### 3.1 现状

| 维度 | 当前系统 | 改进后 |
|------|---------|--------|
| 候选数 | 1 (单模型单策略) | 10 (5 模型 × 2 策略) |
| 选择方式 | 无 / majority vote | 执行聚类 + LLM 仲裁 |
| 模型多样性 | Gemini Flash 为主 | 5 个互补模型 |
| Prompt 多样性 | Decomposed only | Decomposed + Direct |
| 预期准确率 | ~73.9% | ~76-78% (保守估计) |

### 3.2 预期提升来源

| 来源 | 预期提升 | 依据 |
|------|---------|------|
| 多模型互补 | +2-3pp | card_games oracle 实验: 4 模型 60%→68% |
| 执行聚类去重 | +1pp | 消除随机错误 |
| LLM Selector 仲裁 | +1-2pp | Agentar 消融: selector 贡献 1.82pp |
| **合计** | **+3-5pp** | |

---

## 4. 实现计划

### Phase 1: 多模型并行生成框架

- [ ] 统一各模型的 API 调用接口 (OpenAI-compatible)
- [ ] 实现两种 prompt 模板 (Decomposed / Direct)
- [ ] 并行调度 10 个生成任务
- [ ] 结果收集 + 解析

### Phase 2: 执行聚类

- [ ] 实现 `execute_and_cluster()` 函数
- [ ] 处理边界情况: 超时、空结果、类型不一致
- [ ] 结果哈希逻辑 (忽略行顺序? 取决于 ORDER BY)

### Phase 3: Selector 仲裁

- [ ] 实现 listwise selector prompt
- [ ] 阈值调参: majority 直出的最低票数
- [ ] 日志记录: 哪些题走了 selector, selector 是否选对

### Phase 4: 评估 + 调优

- [ ] 在 dev set 上全量评估
- [ ] 分析 selector 介入题的正确率
- [ ] 调整阈值和 prompt

---

## 5. 附录: SOTA Selector Prompt 参考

### A. Agentar Pairwise Prompt

```
You are an advanced SQL evaluation assistant tasked with evaluating
multiple SQL query candidates against a database question.

Key Responsibilities:
- Analyze SQL candidates systematically
- Evaluate against the provided question and database schema
- Consider execution results and data accuracy
- Provide reasoned analysis before selecting

Database Schema: {schema}
Matched Contents: {evidence_values}
Evidence: {evidence}
Question: {question}
SQL Candidates: {numbered_candidates_with_results}

Include your reasoning, then output: \boxed{candidate_number}
```

### B. CHESS Unit Test Generation Prompt

```
Generate unit tests that distinguish the candidate responses from
each other. Each test should distinguish at least two candidates.

Tests evaluate logical correctness of SQL queries, not output
formatting or specific values.

Format: Python list of test strings.
Example: ["The answer should return exactly one row",
          "The query should use a JOIN between table_a and table_b"]
```

### C. XiYan-SQL Candidate Reorganization

```python
def reorganize_candidates(candidates, execution_results, generator_order):
    # 1. 按执行结果聚类
    clusters = group_by_result(candidates, execution_results)
    # 2. 大组排前 (inter-group)
    clusters.sort(key=lambda c: len(c), reverse=True)
    # 3. 组内按 generator 性能排 (intra-group)
    for cluster in clusters:
        cluster.sort(key=lambda c: generator_order[c.model])
    return flatten(clusters)
```
