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
拉取(raw JSON) → 规则预处理 → 会话切分 → LLM 抽取 → 事件合并 → 输出 / 评估 / 看板
   fetch.py       preprocess.py   segment.py   extract.py   merge.py   pipeline.py / evaluate.py / board.py
```

| 阶段 | 做什么 |
|---|---|
| 预处理 | 按 `send_time` 排序；从群名解析客户 UID；用 `is_inner` 区分内部/客户；去掉 OCR 重复采集和噪声消息；标记图片；脱敏；正则抽取模型名、request_id、数量（TPM/QPS/金额…）、渠道、@ 的人；按关键词给出候选事件类型 |
| 切分 | 同一群内，间隔超过 30 分钟就切开；单个窗口最多 60 条消息、12000 字，超出时切开并保留 3 条重叠 |
| 抽取 | 消息用窗口内短编号 `m1..mN` 表示，模型只需引用编号，由代码映射回真实 ID 和时间；用结构化输出直接得到 JSON；校验证据编号，无效编号丢弃，没有有效证据的事件整体丢弃 |
| 合并 | 同群、同类型的事件，证据有交集，或时间相近且标题相似，就合并成一个；状态取时间上更晚的那条，等级取更高的那条 |
| 评估 | 和人工标注对比，按类型输出 P/R/F1，以及状态、等级字段的准确率 |
| 看板 | 把事件渲染成单文件 HTML 看板，按状态/类型/等级/跟进人分列，可筛选、拖拽改状态、查看证据原文 |

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

# 3. 完整抽取（需要 ANTHROPIC_API_KEY）→ out/<uid>_<时间>/events.jsonl、report.md、board.html
aes -v run --input tests/fixtures/sample_response.json

# 4. 和人工标注对比
aes eval --pred out/.../events.jsonl --gold eval/gold/xxx.jsonl

# 5. 把多次运行的结果汇总成一个看板（默认扫描 out/ 下所有 events.jsonl）
aes board out/ --input data/raw/1380000001_2026-11-10_2026-11-12.json --out out/board.html
```

## 事件看板

界面参考 Linear：左侧是视图（全部 / 未关闭 / 高优先级 / 超 24h 无动态 / 未分配）、事件类型和客户群；中间默认是按状态分组的列表，`Ctrl+B` 切到看板；点开事件后，右侧滑出详情，包括证据消息原文、属性、修改动态，以及状态、优先级、类型、跟进人这几个可改的字段。

两种用法：

| | 命令 | 修改保存在 |
|---|---|---|
| 单文件 | `aes run` 自动生成 `board.html`；或 `aes board out/ --input data/raw/xxx.json` 合并多个结果 | 当前浏览器（可导出 JSONL） |
| 服务 | `aes serve --out-dir out --raw-dir data/raw --state data/board_state.json` | 服务端 JSON 文件，多人共享，记录修改人与时间 |

服务模式每次请求都会重新读取 `out/` 下的所有 `events.jsonl`，新跑出的结果刷新页面即可看到；页面每 20 秒同步一次其他人的修改。人工修改单独保存，不会改动 `events.jsonl`。服务只监听 `127.0.0.1`，鉴权交给反向代理（如 Caddy `basic_auth`），代理通过 `X-Remote-User` 头传入用户名，用来记录是谁做的修改。

快捷键：`/` 搜索，`J`/`K` 上下移动，`Enter` 打开，`Esc` 关闭，`S` 改状态，`P` 改优先级，`A` 改跟进人，`T` 改类型，`F` 筛选，`Ctrl+B` 切换列表/看板。

> 看板里含有聊天原文（已脱敏），请只在有鉴权的地址上提供服务，不要提交或外传 `out/`、`data/`。

## 调优入口（比拼的重点）

| 文件 | 作用 |
|---|---|
| `config/event_types.yaml` | 每类事件的定义、算/不算的标准、专属属性、关键词 |
| `src/aftersales_events/prompts/system.md` | 抽取规则：状态、等级、证据、置信度的判断标准 |
| `config/settings.yaml` | 去重窗口、噪声词、切分粒度、模型、effort、合并阈值 |
| `eval/gold/` | 人工标注集（真实数据不入库，只保留示例 `example.jsonl`） |

建议的迭代方式：先取 2 到 3 天、几个群的数据人工标注，之后每改一版 prompt 或配置就跑一遍 `aes run` 和 `aes eval`，用数字比较效果。

## 模型

`llm.provider` 选择抽取用的模型，环境变量 `AES_LLM_PROVIDER` 可以覆盖：

| provider | 调用方式 | 需要的环境变量 |
|---|---|---|
| `claude`（默认） | Anthropic Python SDK 结构化输出（`messages.parse` + Pydantic），默认 `claude-opus-5-5`、effort `medium`，system prompt 缓存，默认开启服务端拒答回退 | `ANTHROPIC_API_KEY` |
| `deepseek` | OpenAI 兼容的 `/chat/completions`（只用标准库），JSON 模式 + 提示词里的格式说明，逐个校验事件，429/5xx/超时自动重试 | `DEEPSEEK_API_KEY`，可选 `DEEPSEEK_BASE_URL` |

`AES_MODEL` 作用于当前选中的 provider，例如：

```bash
AES_LLM_PROVIDER=deepseek AES_MODEL=deepseek-v4-pro DEEPSEEK_BASE_URL=http://127.0.0.1:8080/v1 DEEPSEEK_API_KEY=sk-... aes -v run --input data/raw/xxx.json
```

需要接其他模型时，实现一个带 `extract(window) -> WindowExtraction` 方法的类即可（参考 `extract.py` 里的 `ClaudeExtractor`、`OpenAICompatExtractor` 和 `DryRunExtractor`）；OpenAI 兼容的网关可以直接在 `extract.py` 的 `PROVIDERS` 里加一个名字复用。

## 数据安全

- `data/`、`out/` 和真实标注文件都已加入 `.gitignore`，**不要提交原始聊天数据**。
- 预处理会在内容送给模型之前脱敏：手机号、邮箱、`sk-` 密钥，以及带 `api_key/token/password/密码` 标签的值。

## 路线图

- [x] 第一阶段骨架：拉取 → 预处理 → 切分 → 抽取 → 合并 → 输出 → 评估
- [ ] 人工标注 2 到 3 天的数据，建立基线指标
- [ ] 图片消息：OCR 或多模态识别（目前只标记为 `<图片>`）
- [ ] 跨天增量：维护未关闭事件库，新窗口优先更新已有事件
- [ ] 第二阶段：接入监控数据、账单用量（用量骤降作为流失信号，骤升作为商机或资源需求信号）
- [x] 事件看板：Linear 风格列表/看板、筛选、键盘操作、证据原文；`aes serve` 多人共享修改
- [ ] 二级输出：按事件类型推送给对应角色，生成日报/周报
