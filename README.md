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

## 定时增量处理（aes sync）

`aes sync` 适合每小时运行：拉取全部群（`--uid 0`）最近两天的记录，按天切分成对话窗口。窗口内容没变就复用缓存，只有新增或有新消息的对话才调用模型。每天维护一份有效窗口清单，同一段对话的旧版本结果会被替换。最后跨窗口、跨天合并，写到 `out/live/events.jsonl`，状态写到 `data/sync_status.json`。

- 某个窗口抽取失败不写缓存，下次自动重试，失败期间保留它上一个版本的结果；
- 单次调用模型的窗口数有上限（`sync.max_windows_per_run`），超出部分优先处理最近的对话，其余留到下次；
- 原始文件、缓存和结果保留 `sync.keep_days` 天（默认 30）；
- 同一时间只允许一个 sync 运行。

服务器上的 systemd 配置见 [deploy/](deploy/README.md)。

## 按 UID 处理事件（自动 + 手动）

「全部群」（`uid=0`）里没有带 UID 的客户群，要抽取它们的事件，需要按 UID 处理。两种方式走同一套流程（拉取 → 切分 → 只抽取新增/变化的对话 → 合并），结果和其他群的事件一起显示：

| | 怎么用 | 处理范围 |
|---|---|---|
| 自动 | 「按 UID 查询」页点「开启自动处理」，或在「设置 → 按 UID 自动处理」里加入 UID | 每小时随 `aes sync` 处理最近 2 天；加入时立即补处理最近 7 天 |
| 手动 | 「按 UID 查询」页选择最近 1 / 2 / 7 天，点「立即处理」；不需要先加入自动处理 | 一次性，几秒到几分钟完成，页面上显示排队/处理中/结果 |

- 自动处理的 UID 保存在 `data/runtime_settings.json`，记录谁在什么时候加入/移除；移除后不再更新，已有事件保留到保留期结束。
- 手动任务写进 `data/sync_requests.json`，由 `aes sync --requests` 处理（服务器上由 `aftersales-sync-now.path` 自动触发）。同一个 UID 重复提交会合并；单次最多 7 天，最多 20 个任务排队。
- 结果按「数据源 + 日期」登记，手动处理过的 UID 不会被下一次定时处理覆盖；同一段对话出现在多个 UID 里只抽取一次。手动处理和每小时的 sync 可以同时运行，只在写结果时短暂互斥。
- 命令行：`aes sync --no-watch` 只处理「全部群」；`aes sync --requests` 处理排队的手动任务。

## 人名脱敏

`privacy.pseudonymize_names` 默认开启。送给模型之前：

- 发送人、正文和 `@提及` 里的群成员昵称都换成代号，例如「员工A」「客户B」；名单外的 `@提及` 换成「人员A」；
- 群名换成「客户群 <UID>」；
- 模型返回的标题、摘要、跟进人和属性里的代号再换回真名，看板上显示的是真名。

另外，手机号、邮箱、`sk-` 开头的密钥，以及带 `api_key/token/password/密码` 标签的值，在预处理时就已经脱敏。

局限：正文里出现、但既不是群成员昵称也没被 @ 的人名，识别不到。`aes preview` 导出的 prompt 就是实际发给模型的内容，可以用它检查。

## 事件看板

界面参考 Linear：左侧是视图（全部 / 未关闭 / 高优先级 / 超 24h 无动态 / 未分配）、事件类型和最需要关注的客户群；「全部客户群」页列出每个群的未关闭事件、最高优先级、事件类型分布、消息数和最近活跃时间，点进去就是该群的事件；中间默认是按状态分组的列表，`Ctrl+B` 切到看板；点开事件后，右侧滑出详情，包括证据消息原文、属性、修改动态，以及状态、优先级、类型、跟进人这几个可改的字段。

两种用法：

| | 命令 | 修改保存在 |
|---|---|---|
| 单文件 | `aes run` 自动生成 `board.html`；或 `aes board out/ --input data/raw/xxx.json` 合并多个结果 | 当前浏览器（可导出 JSONL） |
| 服务 | `aes serve --out-dir out --raw-dir data/raw --state data/board_state.json` | 服务端 JSON 文件，多人共享，记录修改人与时间 |

服务模式每次请求都会重新读取 `out/` 下的所有 `events.jsonl`，新跑出的结果刷新页面即可看到；页面每 20 秒同步一次其他人的修改。人工修改单独保存，不会改动 `events.jsonl`。服务只监听 `127.0.0.1`，鉴权交给反向代理（如 Caddy `basic_auth`），代理通过 `X-Remote-User` 头传入用户名，用来记录是谁做的修改。

**聊天记录**：「全部客户群」页每行的聊天图标、某个群的事件列表、事件详情里的「查看完整对话」，都能打开该群的全部消息（包括「收到」这类短消息）。打开后停在最新消息，像微信一样往上划自动加载前一天并保持当前位置，没有消息的日子自动跳过（连续 7 天都没有才停下，可以继续加载）；从事件进入时先定位到证据消息，上下都能继续加载。停在最新消息时每分钟自动拉取新消息。可以跳到某个日期，按客户/内部/事件证据筛选，搜索内容或发送人；作为事件证据的消息会高亮，点击可打开对应事件。消息是打开时通过取数中转（`aes relay`）实时向数据源请求的，只在内存里缓存一两分钟（历史日期半小时），不写磁盘，所以也能看保留期以外的日期。手机号、邮箱、密钥照样脱敏。

**按 UID 查询**：带 UID 的客户群（以及对应的「内部」群）不在 `uid=0`「全部群」的结果里，只有按 UID 向数据源请求才会返回。侧边栏「按 UID 查询」输入用户 UID（如 `1383603926`），实时列出最近 7 天（可切到 31 天）有消息的关联群，标出内部群/客户群、消息数、最近消息时间，以及看板里该 UID 的事件；点击一行打开该群聊天记录（同样按 UID 实时获取，不落盘）。在「全部客户群」的搜索框里输入一串数字、或点事件详情里的「客户 UID」也能跳过来；地址带 `#uid=<UID>`，可以直接分享。这些群默认只能看聊天记录，要抽取事件见下面的「按 UID 处理事件」。

**时区**：`timezone`（默认 `Asia/Shanghai`）决定「今天」是哪天，也决定看板上显示的时间，跟服务器系统时区无关。

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

在看板的「设置」页可以直接切换模型：列表来自网关的 `/models`（每次 `aes sync` 时查询），选择保存在 `data/runtime_settings.json`，并记录修改人和时间。下一次 `aes sync` 生效，优先级高于 `AES_MODEL`；选「默认」就恢复用服务器配置。换模型只影响之后新出现或有新消息的对话，缓存指纹不含模型名，已经处理过的对话不会重跑。需要全部重跑时用 `aes sync --reprocess`。

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
- [x] 跨天增量：`aes sync` 每小时增量处理，窗口缓存 + 跨天合并
- [x] 人名脱敏：送模型前换成代号，结果换回真名
- [ ] 第二阶段：接入监控数据、账单用量（用量骤降作为流失信号，骤升作为商机或资源需求信号）
- [x] 事件看板：Linear 风格列表/看板、筛选、键盘操作、证据原文；`aes serve` 多人共享修改
- [ ] 二级输出：按事件类型推送给对应角色，生成日报/周报
