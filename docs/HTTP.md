# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `POST /api/runs/{id}/corrections`：对已签发批次提交晚到补证，正文为 `{"supplement": [{"trip_id": ..., "pauses": [{"start": ..., "end": ...}]}]}`；返回更正记录，状态为 `ISSUED`、`UNCHANGED` 或 `FAILED`。
- `GET /api/runs/{id}/corrections`：列出批次的更正记录。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。
