# 性能基准 (Performance Benchmark)

测试环境：Windows 11 笔记本（本地 SQLite WAL）、Uvicorn 单进程、Locust 20 并发虚拟用户、30 秒采样。
工具：`locustfile.py`（读多写少负载：列表 6 : 看板 3 : 到期 2 : 语义搜索 1 : 健康检查 1）。

## 基准结果（2026-09-19，268 请求，0 失败）

| 端点 | 请求 | 平均 | 中位 | P95 | 最大 | req/s |
|---|---|---|---|---|---|---|
| GET /api/questions（列表） | 118 | 58ms | **10ms** | 190ms | 1026ms | 4.2 |
| GET /api/stats/dashboard | 65 | 40ms | **12ms** | 130ms | 608ms | 2.3 |
| GET /api/review/due | 28 | 45ms | **10ms** | 120ms | 767ms | 1.0 |
| GET /api/questions?keyword（语义） | 20 | 651ms | 270ms | 3200ms | 3153ms | 0.7 |
| POST /api/auth/login（bcrypt 刻意慢） | 20 | 2343ms | 2300ms | 2400ms | 2427ms | 0.7 |
| **聚合** | **268** | **264ms** | **12ms** | **2300ms** | — | **9.5** |

## 关键结论

1. **核心读接口 P50 ≈ 10ms**：SQL 下推（标签/关键词过滤在数据库完成）+ WAL 模式后，列表与看板在 20 并发下保持个位数至低双位数毫秒。
2. **语义搜索是重尾**（P50 270ms / P95 3.2s）：首查含嵌入模型冷启动；热路径约 250-400ms，主要耗时在本地嵌入计算。生产部署建议接入远程嵌入 API（`EMBEDDING_BASE_URL`）。
3. **登录 2.3s 是设计使然**：bcrypt（12 rounds）+ 失败延迟 1s 是刻意的安全开销，非性能问题。
4. **零失败**：30 秒 20 并发下无错误、无 5xx、无锁等待超时（WAL + busy timeout 生效）。

## 已做的性能工作

- 列表/到期/计数全部 SQL 下推（避免 Python 侧全量过滤）
- SQLite WAL + busy timeout 30s（多线程写安全）
- 图片上传自动压缩（≤1600px JPEG）
- AI 解析异步任务化（不占请求线程）

## 检索评测（Recall@K）

工具：`python -m scripts.eval_retrieval`。方法：以每道错题的标签为查询，检验目标题是否出现在 top-K（18 条查询，2026-09-19）。

| K | 关键词 | 向量 | RRF 融合 |
|---|---|---|---|
| 1 | 44% | 11% | 39% |
| 3 | 61% | 22% | 56% |
| 5 | 72% | 33% | 61% |

**诚实结论**：在本数据集（查询=精确标签、且大量错题共享同一标签，如 8 题同属「一元二次方程」）上，关键词单路已接近上限，RRF 融合反而被弱向量路稀释。

**何时融合有价值**：查询是自然语言（同义词/错别字/跨题描述）而非精确标签时——关键词 LIKE 命中不了，向量路提供独有召回，融合才有增益。因此实现保留为**双路融合可配置**，评测脚本可持续跟踪。

改进方向：本地 ONNX 嵌入对短中文标签区分度低 → 接入远程 bge-m3 嵌入（已支持）或加 rerank 精排（已留钩子）后重测。

## 复现

```bash
uvicorn api.main:app --port 8000
pip install locust
locust -f locustfile.py --host http://localhost:8000 --headless -u 20 -r 5 -t 30s --csv benchmark
```

## 共享题库复用观测

`backend.services.ai.telemetry.summarize()` 在最近记录窗口内分别汇总 AI 调用与题库匹配：

- `question_bank.matches`：`exact` / `similar` / `new` 的匹配次数；相似候选仍需学生明确确认，
  不能把相似命中直接算作复用成功。
- `question_bank.skipped_ai_calls`：只在个人引用成功后按实际路径累计。整图拍照复用计
  `analyze_image`，切题/文档复用计 `analyze_text`；只有该题确有图形工作且既有几何服务确认
  TeX 可用时才计 `redraw_geometry`。无图形、仅查询命中或无 TeX 的裁剪兜底不记该项节省。
  这是调用次数指标，不是响应延迟或费用估算。
- `question_bank.similar_decisions` / `corrections`：分别记录明确接受、明确作为新题和相似纠正
  次数；未确认候选不算接受。
- `question_bank.shared_asset_savings`：按实际复用的共享 AI 图累计 `generations_avoided` 与现存
  文件字节 `storage_bytes_avoided`，不把学生私有证据计入存储节省。
- `token_usage`：仅当提供商在调用上下文中给出真实用量时，聚合实际 prompt/completion/total
  token 和有用量的调用数；没有用量时为 `null`，不按题干长度、历史平均值或模型价格推算。
- SQL `question_bank_audit` 追踪编辑、删除、admin 快照删除和学生孤儿清理；搜索不写审计。
  相似候选阈值由 `BANK_SIMILARITY_THRESHOLD` 配置。`scripts.eval_retrieval.evaluate_bank_matching`
  的独立受控数据集覆盖同题、空白/LaTeX、OCR 噪声、改数字和同知识点异题，分别输出精确准确率、
  相似召回率和误匹配率；该受控评测不代表真实嵌入模型质量。

比较复用效果时应分别报告调用次数、匹配结果、相似确认结果和共享资产复用，不把 MockProvider
的零调用流程解释为真实模型识别准确率或真实 token 账单。向量故障重试、资源清理待办和真实
模型质量需要各自独立验收。
