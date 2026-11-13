# aftersales-events · 售后智能系统（事件清洗）

从多源数据中清洗出需要跟进的业务事件。第一阶段只处理聊天记录，第二阶段接入监控数据和用量变化。

| 事件类型 | 推送对象 |
|---|---|
| 商机 | 销售 |
| 流失 | 内部/销售 |
| 牌照 | 法务/合规/销售（口径待确认） |
| 资源需求 | 采购 |
| 功能需求 | 产品 |
| 商务需求 | 销售 |
| **故障**（建议新增） | 研发/运维 |

> 会议纪要列了六类事件，但二级输出里提到"排障推给研发"，真实数据里也有大量渠道异常、切换渠道的内容，所以默认加了第七类「故障」。不需要的话，在 `config/event_types.yaml` 里设 `enabled: false` 即可。

## 流水线

```
拉取(raw JSON) → 规则预处理 → 会话切分 → LLM 抽取 → 事件合并 → 输出 / 评估
   fetch.py       preprocess.py   segment.py   extract.py   merge.py   pipeline.py / evaluate.py
```

| 阶段 | 做什么 |
|---|---|
| 预处理 | 按 `send_time` 排序；从群名解析客户 UID；用 `is_inner` 区分内部/客户；去掉 OCR 重复采集和噪声消息；标记图片；脱敏；正则抽取模型名、request_id、数量（TPM/QPS/金额…）、渠道、@ 的人；按关键词给出候选事件类型 |
| 切分 | 同一群内，间隔超过 30 分钟就切开；单个窗口最多 60 条消息、12000 字，超出时切开并保留 3 条重叠 |
| 抽取 | 消息用窗口内短编号 `m1..mN` 表示，模型只需引用编号，由代码映射回真实 ID 和时间；用结构化输出直接得到 JSON；校验证据编号，无效编号丢弃，没有有效证据的事件整体丢弃 |
| 合并 | 同群、同类型的事件，证据有交集，或时间相近且标题相似，就合并成一个；状态取时间上更晚的那条，等级取更高的那条 |
| 评估 | 和人工标注对比，按类型输出 P/R/F1，以及状态、等级字段的准确率 |

每个事件的字段：`type, title, summary, status, level, owner, customer_uid, group_*, start_time, end_time, evidence_msg_ids, attributes, confidence`。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

可直接使用仓库中的合成样例离线预览。接入自己的数据源前，请参阅 [数据访问说明](docs/ACCESS.md)。

```bash
# 1. 用合成样例运行规则层和切分，导出 prompt（不调模型）
aes preview --input tests/fixtures/sample_response.json

# 2. 配置自己的数据源后，拉取数据 → data/raw/
AES_FETCH_URL=https://api.example.com/chat-records aes fetch --uid 1380000001 --start 2026-11-10 --end 2026-11-12

# 3. 完整抽取（需要 ANTHROPIC_API_KEY）→ out/<uid>_<时间>/events.jsonl、report.md
aes -v run --input tests/fixtures/sample_response.json

# 4. 和人工标注对比
aes eval --pred out/.../events.jsonl --gold eval/gold/xxx.jsonl
```

如果需要从另一台机器的 Docker 容器访问数据源，可在本地配置 `fetch.mode: docker`、`fetch.container` 和 `fetch.ssh_prefix: ["ssh", "user@host"]`。

## 调优入口（比拼的重点）

| 文件 | 作用 |
|---|---|
| `config/event_types.yaml` | 每类事件的定义、算/不算的标准、专属属性、关键词 |
| `src/aftersales_events/prompts/system.md` | 抽取规则：状态、等级、证据、置信度的判断标准 |
| `config/settings.yaml` | 去重窗口、噪声词、切分粒度、模型、effort、合并阈值 |
| `eval/gold/` | 人工标注集（真实数据不入库，只保留示例 `example.jsonl`） |

建议的迭代方式：先取 2 到 3 天、几个群的数据人工标注，之后每改一版 prompt 或配置就跑一遍 `aes run` 和 `aes eval`，用数字比较效果。

## 模型

默认用 Claude（`claude-opus-5-5`，effort `medium`），通过 Anthropic Python SDK 的结构化输出（`messages.parse` + Pydantic）调用。system prompt 在所有窗口间完全相同，会被缓存。默认开启服务端拒答回退（`server_side_fallback`），可以在配置里关闭。环境变量 `AES_MODEL`、`AES_EFFORT` 可以覆盖配置。

需要换成公司 MaaS 或其他模型时，实现一个带 `extract(window) -> WindowExtraction` 方法的类即可（参考 `extract.py` 里的 `ClaudeExtractor` 和 `DryRunExtractor`）。

## 数据安全

- `data/`、`out/` 和真实标注文件都已加入 `.gitignore`，**不要提交原始聊天数据**。
- 预处理会在内容送给模型之前脱敏：手机号、邮箱、`sk-` 密钥，以及带 `api_key/token/password/密码` 标签的值。

## 路线图

- [x] 第一阶段骨架：拉取 → 预处理 → 切分 → 抽取 → 合并 → 输出 → 评估
- [ ] 人工标注 2 到 3 天的数据，建立基线指标
- [ ] 图片消息：OCR 或多模态识别（目前只标记为 `<图片>`）
- [ ] 跨天增量：维护未关闭事件库，新窗口优先更新已有事件
- [ ] 第二阶段：接入监控数据、账单用量（用量骤降作为流失信号，骤升作为商机或资源需求信号）
- [ ] 二级输出：按事件类型推送给对应角色，生成日报/周报
