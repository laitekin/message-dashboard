# 服务器部署

```
/opt/aftersales-board/
  src/        仓库代码（git）       venv/   Python 虚拟环境
  config/     运行配置（从 src/config 复制）
  .env        模型与数据源配置（root 600，不入库）
  data/raw/   每天的原始聊天记录（保留 30 天）
  data/windows/  窗口抽取缓存 + 每天有效窗口清单
  data/board_state.json  看板上的人工修改与修改记录
  data/sync_status.json  最近一次 aes sync 的状态（看板左下角显示）
  out/live/events.jsonl  aes sync 输出的事件，看板读取
```

| 单元 | 作用 |
|---|---|
| `aftersales-board.service` | 看板服务，只监听 127.0.0.1:8470，由 Caddy 提供 HTTPS 和登录 |
| `aftersales-relay.service` | 取数中转，只监听 127.0.0.1:8472，供看板「聊天记录」和「按 UID 查询」实时拉取（docker exec vpn-proxy），不落盘；看板服务本身不持有 docker 权限 |
| `aftersales-sync.service` + `.timer` | 每小时拉取全部群（UID 0）最近两天的记录，只抽取新增/变化的对话，写入 `out/live/` |

`.env` 示例（值按实际填写）：

```
AES_LLM_PROVIDER=deepseek
AES_MODEL=deepseek-v4-pro
DEEPSEEK_BASE_URL=http://127.0.0.1:8080/v1
DEEPSEEK_API_KEY=...
AES_FETCH_MODE=docker
AES_FETCH_CONTAINER=vpn-proxy
AES_FETCH_URL=https://...
```

更新代码：

```bash
cd /opt/aftersales-board && git -C src pull --ff-only && venv/bin/pip install --quiet --no-deps --force-reinstall ./src && systemctl restart aftersales-relay aftersales-board
```

常用命令：

```bash
systemctl start aftersales-sync          # 立即跑一次
journalctl -u aftersales-sync -n 50      # 看最近一次的日志
systemctl list-timers aftersales-sync.timer
```

改了 `config/event_types.yaml` 或 prompt 后，需要用 `aes sync --reprocess` 让已缓存的对话重新抽取（会重新调用模型）。
