# 数据访问说明

`aes preview --input tests/fixtures/sample_response.json` 可以用合成样例离线验证预处理和会话切分。

## 接入自己的数据源

- 设置环境变量 `AES_FETCH_URL`，或在本地配置中的 `fetch.url` 填入已获授权的接口地址。
- 工具向该地址发送 POST 请求，使用 `Content-Type: application/json;charset=UTF-8`。
- 请求体为 `{"uid":"<客户UID>","start":"YYYY-MM-DD","end":"YYYY-MM-DD"}`。
- 默认从当前机器发请求。若需从容器发请求，将 `fetch.mode` 设置为 `docker`，并配置 `fetch.container`（或环境变量 `AES_FETCH_MODE=docker`、`AES_FETCH_CONTAINER`）；远程容器可另配 `fetch.ssh_prefix`。
- `aes fetch` 将返回数据写入 `data/raw/`。建议先保存，再用 `aes preview --input <文件路径>` 检查处理结果。

## 返回结构

完整示例见 [合成测试数据](../tests/fixtures/sample_response.json)。响应应包含 `code`（成功为 0）、`uid`、`total` 和 `groups`。每个群包含 `group_number`、`group_name` 和 `records`。消息使用 `id` 作为证据编号，按 `send_time` 排序；`is_inner` 为 1 表示内部人员，为 0 表示外部人员。

请将真实聊天记录、输出和人工标注保留在本地，并在向模型发送数据前按实际要求检查脱敏结果。
