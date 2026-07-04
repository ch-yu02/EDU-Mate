# EDU-Mate API Schema

本文档描述当前后端接口契约，供前端、算法模块、硬件采集模块和
mock sender 联调使用。

当前后端职责：

1. 创建课堂 session。
2. 接收外部实时输入：字幕、图片和可选 OCR/多模态分析结果。
3. 在 EDU-Mate 内部生成知识抽取结果。
4. 更新课堂上下文和知识图谱。
5. 通过 WebSocket 推送增量更新。
6. 结束课堂并保存本地文件。

## 1. 基础约定

默认后端地址：

```text
http://127.0.0.1:8000
```

默认 WebSocket 地址：

```text
ws://127.0.0.1:8000/ws/{session_id}
```

HTTP 请求统一使用 JSON：

```http
Content-Type: application/json
```

时间字段分两类：

- `start_time` / `end_time` / `created_at` / `upload_time`：ISO-8601 字符串。
- `start_ts` / `end_ts` / `capture_ts` / `timestamp_range`：课堂内相对时间，单位秒。

课堂 session 存在于内存中。后端重启后，历史文件仍在磁盘，但旧 session
不会自动恢复为可写状态。

## 2. 系统接口

### GET /

检查服务基本信息。

响应示例：

```json
{
  "name": "Lecture-Link Agent Backend",
  "version": "0.1.0",
  "status": "ok"
}
```

### GET /health

健康检查。

响应示例：

```json
{
  "status": "ok"
}
```

## 3. 会话接口

### POST /sessions/start

创建一节课堂。后端会自动生成 `session_id`，并初始化课堂上下文和知识图谱。

请求体：

```json
{
  "title": "通信原理第8讲：傅里叶变换",
  "course": "通信原理",
  "teacher": "张老师",
  "language": "zh-CN",
  "created_by": "student",
  "device_id": "dk2500_001"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 否 | 课堂标题，默认 `"未命名课堂"` |
| `course` | string/null | 否 | 课程名称 |
| `teacher` | string/null | 否 | 教师姓名 |
| `language` | string | 否 | 语言代码，默认 `"zh-CN"` |
| `created_by` | string | 否 | 创建者，默认 `"student"` |
| `device_id` | string/null | 否 | 设备 ID |

响应状态码：`201 Created`

响应体 `LectureSession`：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "title": "通信原理第8讲：傅里叶变换",
  "course": "通信原理",
  "teacher": "张老师",
  "start_time": "2026-06-05T01:02:03.000000+00:00",
  "end_time": null,
  "status": "recording",
  "language": "zh-CN",
  "created_by": "student",
  "device_id": "dk2500_001"
}
```

### PATCH /sessions/{session_id}

更新课堂标题和课程名称。用于前端手动改名，也用于 WhisperLive/Qwen 最终笔记
生成后由云端 notes-agent 把推断出的课堂名称同步到后端。本地 Qwen 只负责
结构化笔记，不负责最终课堂命名。

请求体：

```json
{
  "title": "傅里叶变换专题",
  "course": "信号与系统"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 否 | 新课堂标题；传空字符串会返回 400 |
| `course` | string/null | 否 | 新课程名称；传 `null` 或空字符串表示清空课程名称 |

响应体：更新后的 `LectureSession`。录制中的课堂会广播 `session.updated`。

### GET /sessions/{session_id}

读取课堂元信息。优先读取内存中的课堂；如果后端重启导致内存丢失，会回退
读取本地历史文件中的 `metadata.json`。

响应状态码：`200 OK`

响应体：`LectureSession`

错误：

| 状态码 | 场景 |
| --- | --- |
| `404` | session 不存在，且本地历史文件中也没有对应元信息 |

### GET /sessions

读取已保存的历史课堂列表。仅返回已经结束并写入 `data/sessions/{session_id}`
的课堂；正在录制但尚未保存的课堂不会出现在列表中。

响应状态码：`200 OK`

响应体：

```json
{
  "sessions": [
    {
      "session": {
        "session_id": "lec_20260605_010203_ab12cd34",
        "title": "通信原理第8讲：傅里叶变换",
        "course": "通信原理",
        "teacher": "张老师",
        "start_time": "2026-06-05T01:02:03.000000+00:00",
        "end_time": "2026-06-05T02:30:00.000000+00:00",
        "status": "ended",
        "language": "zh-CN",
        "created_by": "student",
        "device_id": "dk2500_001"
      },
      "event_count": 12,
      "storage_path": "data/sessions/lec_20260605_010203_ab12cd34"
    }
  ]
}
```

### GET /sessions/recording

读取当前后端内存中仍处于 `recording` 状态的课堂，按开始时间倒序返回。

用途：

- 本地音频/WhisperLive 联调脚本自动接入前端已经开始的课堂。
- 前端接入脚本自动创建或正在录制的测试课堂。
- 避免每次测试都手动复制 `session_id`。

响应状态码：`200 OK`

响应体：

```json
[
  {
    "session_id": "lec_20260618_034336_c171e7e5",
    "title": "本地音频测试课堂",
    "course": null,
    "teacher": null,
    "start_time": "2026-06-18T03:43:36.000000+00:00",
    "end_time": null,
    "status": "recording",
    "language": "zh-CN",
    "created_by": "student",
    "device_id": null
  }
]
```

### GET /sessions/{session_id}/history

读取一节已保存课堂的完整历史内容，用于历史回放、课后技能和总结页面。

响应状态码：`200 OK`

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `session` | `LectureSession` | 课堂元信息 |
| `transcript_markdown` | string | `transcript.md` 的完整内容 |
| `structured_notes_markdown` | string/null | `structured_notes.md` 的完整内容；旧课堂可能为空 |
| `timeline` | `TimelineItem[]` | `timeline.json` 的时间线条目 |
| `knowledge_graph` | `KnowledgeTree` | `knowledge_graph.json` 的图谱快照 |
| `storage_path` | string | 本地历史课堂目录 |
| `post_class_artifacts` | object | `summary.md`、`todos.json`、`quiz.json`、Agent artifact 和消息记录 |
| `post_class_status` | string | `idle` / `generating` / `ready` / `failed` |
| `post_class_warnings` | string[] | 课后产物生成提示或失败原因 |
| `knowledge_graph_status` | string | `finalizing` / `final` / `failed`；`final` 表示最终图谱已经基于 final notes 更新完成 |

错误：

| 状态码 | 场景 |
| --- | --- |
| `404` | 本地历史课堂不存在或缺少必要文件 |

### DELETE /sessions/{session_id}/history

删除一节已保存课堂的本地历史目录。只影响磁盘历史数据，不会把已经结束的
内存课堂恢复或重开。如果该课堂的课后产物后台任务仍在运行，后端会取消该
任务并阻止它继续写文件；即使底层线程稍后返回，也不会把已删除的课堂目录
重新创建出来。

响应：

```json
{
  "status": "deleted",
  "session_id": "lec_20260618_034336_c171e7e5"
}
```

### POST /sessions/{session_id}/end

结束课堂。该接口是幂等的，重复结束同一节课不会产生重复事件语义。

处理流程：

1. 读取课堂上下文。
2. 读取知识图谱。
3. 将 session 状态改为 `ended`。
4. 先保存核心本地文件，包括 `structured_notes.md`（如果录制中收到过结构化笔记）。
5. 写入 `post_class_status.json`，状态为 `generating`。
6. 通过 WebSocket 广播 `session.ended`，其中包含保存路径和 `post_class_status: "generating"`。
7. 立刻返回结束后的 `LectureSession`，不等待 Qwen final notes、summary /
   todos、final graph 或 RAG 索引。
8. 后台任务先等待或调用本地 Qwen 生成 final `structured_notes.md`；完成后
   广播 `post_class.updated`，`post_class_stage="notes_ready"`。
9. 随后 summary/todos/title/RAG artifacts 和 final notes graph update 分别
   作为独立后台阶段运行；谁完成就先广播对应的 `post_class.updated`。
10. `post_class_status` 只描述课后产物阶段；`knowledge_graph_status` 单独描述
   最终图谱阶段。只有 final notes graph update 成功后，
   `knowledge_graph_status` 才会变成 `"final"`。
11. 单节课 RAG 索引只有在 `RAG_QUERY_BACKEND=llamaindex` 且
   `POST_CLASS_BUILD_RAG_INDEX=1` 时自动构建。

如果程序在 `session.ended` 后、`post_class.updated` 前关闭，核心课堂文件已经
保存，但进程内后台任务会丢失。重启后读取该历史课堂时，后端会发现
`post_class_status.json` 仍为 `generating` 且当前进程没有对应任务，于是把
状态标记为 `failed`，避免前端永久显示“生成中”。当前版本不会自动恢复这类
中断任务。

响应状态码：`200 OK`

响应体：结束后的 `LectureSession`

错误：

| 状态码 | 场景 |
| --- | --- |
| `404` | session、context 或 knowledge graph 不存在 |

## 4. Agent 接口

### POST /agent/chat

对当前录制中课堂或已保存历史课堂进行问答、总结、待办提取和自测题生成。

请求：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "prompt": "采样定理为什么重要？",
  "mode": "auto",
  "answer_mode": "grounded"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | 课堂 ID，可以是录制中课堂或历史课堂 |
| `prompt` | string | 是 | 用户自然语言输入 |
| `mode` | string | 否 | `auto` / `qa` / `summary` / `todos` / `quiz`，默认 `auto` |
| `answer_mode` | string | 否 | 只对 QA 生效。`strict` 只依据课堂资料；`grounded` 允许模型补充通用解释 |

响应：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "intent": "qa",
  "answer": "根据课堂内容：...\n补充解释：...",
  "artifacts": [],
  "source_refs": [
    {
      "type": "segment",
      "id": "seg_001",
      "ts": 1.0,
      "text": "采样定理说明..."
    }
  ],
  "warnings": [
    "回答包含模型通用知识补充；课堂依据见来源引用。"
  ]
}
```

说明：

- `strict` 是默认答疑模式，适合复习、笔记和考试场景。
- `strict` 有课堂来源且配置 LLM 时，会让模型把来源整理成自然回答；提示词
  禁止使用来源外信息。未配置或失败时回退为检索摘要。
- `grounded` 仍要求先找到课堂来源；没有课堂依据时不会退化成开放域问答。
- `summary` / `todos` / `quiz` 会忽略 `answer_mode`，继续由各自技能控制是否使用 LLM。

### POST /agent/search

跨课堂搜索已保存历史课堂。

请求：

```json
{
  "query": "哪节课讲过采样定理",
  "course": "通信原理",
  "date_from": "2026-06-01",
  "date_to": "2026-06-09",
  "limit": 8
}
```

响应核心字段：

- `answer`：搜索结果摘要。
- `hits`：命中来源，包含 `session_id`、课堂标题、课程、分数和 `source_ref`。
- `warnings`：坏历史目录跳过、LlamaIndex 回退等非致命提示。

全局索引可手动重建：

```bash
scripts/dev.sh rebuild-global-index
scripts/dev.sh rebuild-global-index --llamaindex
```

不带 `--llamaindex` 时只重建可审计的 `documents.json` 快照；带参数时会同时
尝试重建 `data/indexes/global/llama_index/`。

### POST /agent/review

基于跨课堂检索来源做课后复习问答。请求体与 `/agent/search` 相同，返回
同样的 `GlobalSearchResponse`。区别是：如果已配置 LLM，后端会把命中的
历史课堂来源整理成自然复习回答；未配置或失败时回退为搜索摘要，并在
`warnings` 中说明。

```json
{
  "query": "复习采样定理",
  "course": "通信原理",
  "limit": 8
}
```

### GET /agent/courses

按课程聚合已保存历史课堂，供课后复习入口使用。

响应：

```json
{
  "courses": [
    {
      "course": "通信原理",
      "session_count": 3,
      "latest_session_id": "lec_20260618_034336_c171e7e5",
      "latest_title": "通信原理第8讲",
      "latest_start_time": "2026-06-18T03:43:36+08:00",
      "node_count": 18,
      "edge_count": 12
    }
  ],
  "warnings": []
}
```

### GET /agent/courses/{course}/knowledge-tree

合并同一课程下多节历史课堂的知识图谱，按节点 label 和边关系去重。该接口
返回的是课后复习用的合并快照，不会修改原始课堂历史文件。

响应核心字段：

- `course`：课程名。
- `session_count`：参与合并的历史课堂数量。
- `knowledge_graph`：合并后的 `KnowledgeTree`。
- `warnings`：损坏历史目录等非致命提示。

### POST /agent/knowledge-tree/update-from-notes

用结构化 Markdown 课堂笔记更新录制中课堂的知识图谱。

这是 WhisperLive/Qwen 本地笔记链路对接云端知识树 Agent 的入口：

```text
WhisperLive 字幕草稿 -> 本地 Qwen streaming structured_notes.md
-> POST /agent/knowledge-tree/update-from-notes
-> 云端 LLM 生成 KnowledgeExtraction
-> 复用 knowledge.extraction 事件管线更新图谱和前端
-> 结束课堂后，后端后台调用本地 Qwen 生成 final structured_notes.md
-> final 快照可同时返回 session_title/course 并广播 session.updated
```

请求：

```json
{
  "session_id": "lec_20260618_034336_c171e7e5",
  "snapshot_id": "notes_000003_streaming",
  "sequence": 3,
  "markdown": "# 课堂笔记\n\n## 课堂要点\n- 傅里叶变换用于频域分析。",
  "markdown_hash": "optional_hash",
  "source_segments": [
    {
      "segment_id": "seg_001",
      "start_ts": 1.0,
      "end_ts": 4.2,
      "text": "傅里叶变换可以把时域信号转换到频域。"
    }
  ],
  "recent_source_segments": [
    {
      "segment_id": "seg_001",
      "start_ts": 1.0,
      "end_ts": 4.2,
      "text": "傅里叶变换可以把时域信号转换到频域。"
    }
  ],
  "update_status": "streaming"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | 必须是录制中课堂 |
| `snapshot_id` | string | 是 | 本次笔记快照 ID |
| `sequence` | number | 否 | 快照序号，用于日志和排查乱序 |
| `markdown` | string | 是 | 当前完整结构化课堂笔记 |
| `markdown_hash` | string/null | 否 | 去重 hash；为空时后端按 Markdown 文本计算 |
| `source_segments` | object[] | 否 | 生成笔记时用到的全量字幕来源 |
| `recent_source_segments` | object[] | 否 | 本次增量优先依据的近期字幕；为空时回退到 `source_segments` |
| `update_status` | string | 否 | `streaming` / `final` |

说明：

- `streaming` 快照主要用于增量图谱更新。
- `final` 快照可同时让云端 notes-agent 根据课堂内容生成短标题和课程名。
- 本地 Qwen 不再润色或替换前端实时字幕；前端字幕显示 WhisperLive/ASR 原始
  `transcript.segment`。

响应：

```json
{
  "status": "applied",
  "session_id": "lec_20260618_034336_c171e7e5",
  "snapshot_id": "notes_000003_streaming",
  "markdown_hash": "computed_or_supplied_hash",
  "extraction_id": "ext_notes_lec_xxx_notes_000003_streaming_abcd1234",
  "graph_patch_operations": 4,
  "warnings": []
}
```

`status` 含义：

| status | 说明 |
| --- | --- |
| `applied` | 生成了有效抽取，并产生图谱增量 |
| `skipped` | 内容重复、没有新增知识或没有图谱操作 |
| `failed` | 云端 LLM 调用或 schema 校验失败 |

成功产生图谱增量时，后端还会广播标准 `event.received` WebSocket 消息，
`event_type` 为 `knowledge.extraction`，前端继续按现有 `graph_patch`
逻辑更新图谱。

### POST /agent/visual/analyze

读取一张已经上传并进入课堂上下文的图片，调用云端多模态 LLM 分析画面内容，
然后回写视觉区并可选更新知识图谱。请求和响应示例见
[6.2.2 多模态图片分析](#622-多模态图片分析)。

该接口用于当前前端摄像头拍照链路，也可被开发板硬件摄像头服务复用。

## 5. 实时事件接口

### POST /events

接收一条课堂实时事件。外部模块主要发送 ASR 和视觉输入；知识抽取由
EDU-Mate 项目内部完成，`knowledge.extraction` 主要作为内部事件、mock
数据和调试格式使用。

统一请求信封 `RealtimeEvent`：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "event_type": "transcript.segment",
  "payload": {}
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | 所属课堂 ID |
| `event_type` | string | 是 | 当前支持三种事件类型 |
| `payload` | object | 否 | 具体事件内容 |

支持的 `event_type`：

```text
transcript.segment
image.capture
knowledge.extraction
```

对外部联调方来说，必须发送的是 `transcript.segment` 和 `image.capture`。
`knowledge.extraction` 不要求外部算法组发送。

响应状态码：`202 Accepted`

响应体：

```json
{
  "status": "accepted",
  "session_id": "lec_20260605_010203_ab12cd34",
  "event_type": "transcript.segment",
  "event_count": 1
}
```

错误：

| 状态码 | 场景 |
| --- | --- |
| `400` | payload 无法解析，或 event_type 不支持 |
| `404` | session、context 或 knowledge graph 不存在 |
| `409` | session 已结束，不再接收实时事件 |

### POST /events/transcript-preview

接收 ASR 的临时识别结果，只通过 WebSocket 推送给前端展示为一条可替换的
“正在识别”字幕。该接口不写入 `ClassroomContext.transcript`、timeline、
结构化笔记、知识图谱或历史文件。

请求体：

```json
{
  "session_id": "lec_20260624_102315_ba9b09e3",
  "payload": {
    "segment_id": "seg_partial_001",
    "start_ts": 12.0,
    "end_ts": 14.0,
    "text": "临时识别中的字幕",
    "speaker": "teacher",
    "is_final": false,
    "source": "whisperlive_openvino"
  }
}
```

响应状态码：`202 Accepted`

响应体：

```json
{
  "status": "accepted",
  "session_id": "lec_20260624_102315_ba9b09e3",
  "event_type": "transcript.preview",
  "event_count": 3
}
```

广播消息类型：`transcript.preview`

### POST /events/transcript-preview/finalize

把当前前端仍在显示的最后一条有效 preview 提升为正式
`transcript.segment`。前端在调用 `POST /sessions/{session_id}/end` 前使用
该接口，避免结束课堂时丢失已经展示给用户但尚未等到 WhisperLive final 的
字幕。

请求体与 preview 相同：

```json
{
  "session_id": "lec_20260624_102315_ba9b09e3",
  "payload": {
    "segment_id": "seg_partial_001",
    "start_ts": 12.0,
    "end_ts": 14.0,
    "text": "临时识别中的字幕",
    "speaker": "teacher",
    "is_final": false,
    "source": "whisperlive_openvino"
  }
}
```

后端会强制写入：

- `is_final=true`
- `session_id` 与外层一致
- 缺失 `segment_id` 时按时间戳生成 `seg_preview_final_*`
- `skip_realtime_extraction=true`，避免结束瞬间触发额外实时抽取

响应和 WebSocket 广播与普通 `transcript.segment` 一致，因此它会进入
`transcript.md`、timeline 和课后上下文。该接口只补写当前最后一条 preview，
不会保存所有历史 preview，避免写入大量重复的中间识别结果。

## 6. 事件 Payload

### 6.1 transcript.segment

ASR 模块发送的实时字幕片段。

示例：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "event_type": "transcript.segment",
  "payload": {
    "segment_id": "seg_001",
    "session_id": "lec_20260605_010203_ab12cd34",
    "start_ts": 1.0,
    "end_ts": 4.2,
    "text": "傅里叶变换可以把时域信号转换到频域进行分析。",
    "speaker": "teacher",
    "confidence": 0.95,
    "is_final": true,
    "source": "whisper"
  }
}
```

payload 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `segment_id` | string | 否 | 字幕片段 ID，缺省时后端补齐 |
| `session_id` | string | 否 | 所属课堂 ID，缺省时后端用外层 session_id |
| `start_ts` | number | 否 | 开始时间，单位秒 |
| `end_ts` | number | 否 | 结束时间，单位秒 |
| `text` | string | 否 | 字幕文本 |
| `speaker` | string/null | 否 | 说话人，默认 `"teacher"` |
| `confidence` | number/null | 否 | ASR 置信度，0 到 1 |
| `is_final` | boolean | 否 | 是否最终结果，默认 `true` |
| `source` | string/null | 否 | 来源模块，例如 `"whisper"` |
| `created_at` | string | 否 | 创建时间，缺省时后端补齐 |

### 6.2 image.capture

摄像头、屏幕截图、OCR 或多模态分析模块发送的视觉事件。当前前端已经内置
浏览器摄像头预览和拍照入口：点击视觉区的“拍照”按钮或按 `Ctrl+1` 会上传
图片、发送 `status=processing` 的 `image.capture`，随后调用云端多模态分析。

示例：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "event_type": "image.capture",
  "payload": {
    "image_id": "img_001",
    "session_id": "lec_20260605_010203_ab12cd34",
    "capture_ts": 10.5,
    "image_path": "local://sessions/lec_xxx/images/img_001.jpg",
    "source": "camera",
    "image_type": "slide",
    "status": "processed",
    "ocr_text": "X(f)=∫x(t)e^{-j2πft}dt",
    "caption": "课件展示傅里叶变换公式。",
    "visual_text": ["X(f)=∫x(t)e^{-j2πft}dt"],
    "key_points": ["傅里叶变换用于把时域信号转换到频域。"]
  }
}
```

payload 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `image_id` | string | 否 | 图片 ID，缺省时后端补齐 |
| `session_id` | string | 否 | 所属课堂 ID |
| `capture_ts` | number | 否 | 捕获时间，单位秒 |
| `upload_time` | string | 否 | 上传时间，缺省时后端补齐 |
| `image_path` | string | 否 | 图片路径，缺省时后端生成 local 路径 |
| `source` | string/null | 否 | 来源，例如 `camera`、`screen_share` |
| `image_type` | string/null | 否 | 图片类型，例如 `slide`、`whiteboard` |
| `status` | string | 否 | 处理状态，默认 `processed` |
| `ocr_text` | string/null | 否 | OCR 文本 |
| `caption` | string/null | 否 | 多模态模型图片描述 |
| `visual_text` | string[] | 否 | 多模态模型直接读到的关键文字/公式片段 |
| `key_points` | string[] | 否 | 多模态模型总结出的课堂视觉要点 |

### 6.2.1 图片上传与读取

如果前端需要显示真实图片，硬件/相机模块可以先上传原始图片 bytes：

```text
PUT /sessions/{session_id}/images/{image_id}
Content-Type: image/jpeg | image/png | image/webp
```

请求体是图片二进制内容，不使用 multipart。响应：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "image_id": "img_001",
  "image_path": "local://sessions/lec_20260605_010203_ab12cd34/images/img_001.jpg"
}
```

随后 `image.capture.payload.image_path` 应使用这个 `image_path`。前端或调试工具
可以读取：

```text
GET /sessions/{session_id}/images/{image_id}
```

后端只服务 `data/sessions/{session_id}/images/` 下的文件，不会直接暴露任意
本机绝对路径。

当前图像处理链路：

1. 相机/硬件/视觉分析模块可先上传原始图片 bytes，得到受控的 `local://`
   `image_path`。
2. 模块发送 `image.capture`，至少携带 `image_path` 和 `capture_ts`。内置
   浏览器拍照会先发送 `status=processing`；外部模块也可以直接携带
   `ocr_text`、`caption`、`visual_text` 或 `key_points`。
3. 后端把视觉事件写入 `ClassroomContext.visuals` 和 timeline，并通过
   WebSocket 推给前端图片/视觉分析区。
4. 若需要跳过 OCR 并直接让云端多模态模型读图，调用
   `POST /agent/visual/analyze`。成功后后端会更新同一个 `image.capture`，
   并把图片中的知识点作为 `knowledge.extraction` 推送给图谱。
5. 录制中的前端若遇到多模态分析失败，会短延迟自动重试，并在重试请求中
   使用 `force=true`；后端会根据同一张图片此前失败次数适度放宽本次云端
   LLM timeout。课堂结束或切换 session 后不会继续重试。
6. Agent/RAG 检索会把视觉来源作为短 source ref 返回；前端可展示真实图片、
   OCR/caption、`visual_text` 和 `key_points`。

### 6.2.2 多模态图片分析

```text
POST /agent/visual/analyze
```

请求体：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "image_id": "img_001",
  "force": false
}
```

响应：

```json
{
  "status": "applied",
  "session_id": "lec_20260605_010203_ab12cd34",
  "image_id": "img_001",
  "caption": "课件展示傅里叶变换公式。",
  "visual_text": ["X(f)=∫x(t)e^{-j2πft}dt"],
  "key_points": ["傅里叶变换用于把时域信号转换到频域。"],
  "extraction_id": "ext_visual_img_001_abc123",
  "graph_patch_operations": 3,
  "warnings": []
}
```

约束：

- `session_id` 必须是录制中的课堂。
- 图片必须已经通过 `PUT /sessions/{session_id}/images/{image_id}` 保存，并且
  已有同名 `image.capture` 进入上下文。
- 成功时会广播一条更新后的 `image.capture`；如果模型返回实体/关系，还会广播
  一条 `knowledge.extraction` 和对应 `graph_patch`。
- 未配置云端多模态 LLM 时返回 `failed`，并把该图片状态更新为 `failed`。

### 6.3 knowledge.extraction

EDU-Mate 内部知识抽取模块生成的一次实体/关系抽取结果。外部模块正常联调
时不需要发送该事件；它保留为内部管线、mock sender 和后端调试使用。

示例：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "event_type": "knowledge.extraction",
  "payload": {
    "extraction_id": "ext_001",
    "session_id": "lec_20260605_010203_ab12cd34",
    "source_segment_ids": ["seg_001"],
    "source_visual_ids": ["img_001"],
    "timestamp_range": [1.0, 10.5],
    "entities": [
      {
        "entity_id": "node_fourier_transform",
        "name": "傅里叶变换",
        "type": "concept",
        "description": "将信号从时域表示转换为频域表示的数学工具"
      },
      {
        "entity_id": "node_frequency_domain",
        "name": "频域",
        "type": "concept",
        "description": "从频率成分角度描述信号的表示方式"
      }
    ],
    "relations": [
      {
        "source": "傅里叶变换",
        "target": "频域",
        "relation": "maps_to"
      }
    ],
    "importance": 0.92
  }
}
```

payload 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `extraction_id` | string | 否 | 抽取 ID，缺省时上下文层会补齐 |
| `session_id` | string | 否 | 所属课堂 ID，图谱层需要它 |
| `source_segment_ids` | string[] | 否 | 来源字幕片段 ID |
| `source_visual_ids` | string[] | 否 | 来源图片 ID |
| `timestamp_range` | [number, number]/null | 否 | 来源时间范围，单位秒 |
| `entities` | object[] | 否 | 实体列表 |
| `relations` | object[] | 否 | 关系列表 |
| `importance` | number/null | 否 | 重要度，0 到 1 |

`entities` 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `entity_id` | string/null | 否 | 实体 ID |
| `name` | string | 是 | 实体名称，用于图谱去重 |
| `type` | string | 否 | 类型，默认 `concept` |
| `description` | string/null | 否 | 实体说明 |

`relations` 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source` | string | 是 | 起点实体名称 |
| `target` | string | 是 | 终点实体名称 |
| `relation` | string | 是 | 关系类型 |

注意：知识图谱节点按实体 `name` 规范化后去重。若 relation 引用了
entities 中没有出现的实体，后端会自动创建占位节点。

## 7. WebSocket

### WS /ws/{session_id}

前端订阅某节课堂的实时更新。

连接要求：

- `session_id` 必须存在于当前后端内存中。
- 如果 session 不存在，后端会用 WebSocket code `1008` 关闭连接。
- 当前 WebSocket 只用于后端推送，客户端发来的消息会被忽略。

连接成功后，后端立即推送：

```json
{
  "type": "ws.connected",
  "session_id": "lec_20260605_010203_ab12cd34",
  "data": {
    "message": "connected"
  },
  "created_at": "2026-06-05T01:02:03.000000+00:00"
}
```

所有 WebSocket 消息都使用统一信封：

```json
{
  "type": "event.received",
  "session_id": "lec_20260605_010203_ab12cd34",
  "data": {},
  "created_at": "2026-06-05T01:02:03.000000+00:00"
}
```

### session.started

调用 `POST /sessions/start` 后广播。

```json
{
  "type": "session.started",
  "session_id": "lec_20260605_010203_ab12cd34",
  "data": {
    "session": {
      "session_id": "lec_20260605_010203_ab12cd34",
      "title": "通信原理第8讲：傅里叶变换",
      "status": "recording"
    }
  },
  "created_at": "2026-06-05T01:02:03.000000+00:00"
}
```

通常创建课堂时还没有前端订阅者，因此前端不应依赖一定能收到这条消息。

### event.received

调用 `POST /events` 成功后广播。

```json
{
  "type": "event.received",
  "session_id": "lec_20260605_010203_ab12cd34",
  "data": {
    "event_type": "knowledge.extraction",
    "payload": {},
    "event_count": 3,
    "context_update": {
      "session_id": "lec_20260605_010203_ab12cd34",
      "event_type": "knowledge.extraction",
      "timeline_item": {
        "item_id": "ext_001",
        "session_id": "lec_20260605_010203_ab12cd34",
        "type": "knowledge",
        "ts": 1.0,
        "title": "知识点：傅里叶变换、频域",
        "data": {}
      },
      "transcript_count": 2,
      "visual_count": 1,
      "knowledge_extraction_count": 1
    },
    "graph_patch": {
      "session_id": "lec_20260605_010203_ab12cd34",
      "from_version": 0,
      "to_version": 1,
      "operations": [
        {
          "op": "add_node",
          "node": {
            "node_id": "node_fourier_transform",
            "label": "傅里叶变换",
            "type": "concept",
            "summary": "将信号从时域表示转换为频域表示的数学工具",
            "level": 0,
            "importance": 0.92,
            "source_refs": [
              {
                "type": "segment",
                "id": "seg_001",
                "ts": 1.0
              }
            ]
          },
          "edge": null,
          "data": {}
        }
      ]
    }
  },
  "created_at": "2026-06-05T01:02:03.000000+00:00"
}
```

说明：

- `context_update.timeline_item` 可写入前端本地状态；当前主界面不再单独显示事件面板，
  但 timeline 仍用于历史保存和后续定位。
- `graph_patch` 只有 `knowledge.extraction` 事件会产生；字幕和图片事件通常为 `null`。
- 前端应按 `graph_patch.operations` 顺序应用图谱变更。
- 字幕或图片事件如果触发内部批量抽取，`data.knowledge_extraction` 会包含
  本次抽取摘要；真正的图谱变更仍会随后以独立的 `knowledge.extraction`
  `event.received` 消息广播。

`data.knowledge_extraction` 示例：

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "provider": "llm",
  "extraction_count": 1,
  "processed_source_ids": ["seg_001", "seg_002", "seg_003"],
  "errors": [],
  "applied": [
    {
      "extraction_id": "ext_lec_xxx_seg_001_seg_002_seg_003",
      "graph_patch_operations": 3
    }
  ]
}
```

知识抽取使用 LLM-backed extractor，复用后端 LLM 环境变量：

```text
LLM_PROVIDER
LLM_API_KEY
LLM_MODEL
LLM_BASE_URL
LLM_TIMEOUT_SECONDS
LLM_MAX_RETRIES
```

如果 LLM 未配置、调用失败或返回内容无法校验为 `KnowledgeExtraction`，
后端会在 `knowledge_extraction.errors` 中返回错误信息，不会自动回退规则版，
也不会把无效抽取写入图谱。

### session.ended

调用 `POST /sessions/{session_id}/end` 后广播。

```json
{
  "type": "session.ended",
  "session_id": "lec_20260605_010203_ab12cd34",
  "data": {
    "session": {
      "session_id": "lec_20260605_010203_ab12cd34",
      "status": "ended"
    },
    "storage": {
      "session_dir": "data/sessions/lec_20260605_010203_ab12cd34",
      "files": {
        "metadata": "data/sessions/lec_20260605_010203_ab12cd34/metadata.json",
        "transcript": "data/sessions/lec_20260605_010203_ab12cd34/transcript.md",
        "structured_notes": "data/sessions/lec_20260605_010203_ab12cd34/structured_notes.md",
        "timeline": "data/sessions/lec_20260605_010203_ab12cd34/timeline.json",
        "knowledge_graph": "data/sessions/lec_20260605_010203_ab12cd34/knowledge_graph.json"
      },
      "post_class_files": {},
      "post_class_status_file": "data/sessions/lec_20260605_010203_ab12cd34/post_class_status.json",
      "rag_index": {
        "enabled": false,
        "status": "pending"
      },
      "knowledge_extraction": {
        "session_id": "lec_20260605_010203_ab12cd34",
        "status": "pending"
      },
      "post_class_status": "generating",
      "knowledge_graph_status": "finalizing"
    }
  },
  "created_at": "2026-06-05T01:02:03.000000+00:00"
}
```

### post_class.updated

`POST /sessions/{session_id}/end` 返回后，后端后台任务分阶段广播
`post_class.updated`。常见阶段：

| `post_class_stage` | 含义 |
| --- | --- |
| `notes_ready` | 本地 Qwen final 结构化笔记已经写入 `structured_notes.md` |
| `artifacts_ready` | `summary.md`、`todos.json`、课堂命名和可选 RAG 索引阶段完成 |
| `graph_ready` | final notes graph update 完成；成功时 `knowledge_graph_status="final"` |
| `done` | 已收到后台阶段的最终汇总状态 |
| `failed` | 后台任务整体异常 |

单节课 RAG 索引只有在 `POST_CLASS_BUILD_RAG_INDEX=1` 时自动构建。示例：

```json
{
  "type": "post_class.updated",
  "session_id": "lec_20260605_010203_ab12cd34",
  "data": {
    "status": "ready",
    "post_class_stage": "artifacts_ready",
    "knowledge_graph_status": "finalizing",
    "post_class_steps": {
      "final_notes": {
        "status": "ready",
        "elapsed_seconds": 18.2
      },
      "artifacts": {
        "status": "ready",
        "elapsed_seconds": 11.4
      },
      "final_graph": {
        "status": "running"
      }
    },
    "post_class_artifacts": {
      "summary_markdown": "本节课主要讲解……",
      "todos": [],
      "quiz": [],
      "agent_artifacts": [],
      "agent_messages": []
    },
    "storage": {
      "final_structured_notes": {
        "status": "ready",
        "elapsed_seconds": 18.2
      },
      "post_class_files": {
        "summary": "data/sessions/lec_20260605_010203_ab12cd34/summary.md",
        "todos": "data/sessions/lec_20260605_010203_ab12cd34/todos.json"
      },
      "rag_index": {
        "enabled": false,
        "status": "skipped"
      }
    },
    "warnings": []
  },
  "created_at": "2026-06-05T01:02:06.000000+00:00"
}
```

`graph_ready` 消息会在 `storage.final_graph` 中包含 notes-agent 图谱更新结果；
如果 final graph 失败，`knowledge_graph_status` 为 `"failed"`，但已经完成的
summary/todos 仍然可以展示。失败时 `data.status` 为 `"failed"`，`warnings`
会包含失败原因；课堂核心文件已经在 `session.ended` 前保存。
后台任务成功、失败或被中断后，历史详情 API 都会通过 `post_class_status`
和 `knowledge_graph_status` 返回当前状态。

## 8. 知识图谱数据

完整知识图谱 `KnowledgeTree` 会在结束课堂时保存为
`knowledge_graph.json`。

```json
{
  "session_id": "lec_20260605_010203_ab12cd34",
  "version": 1,
  "root_nodes": ["node_fourier_transform"],
  "nodes": [
    {
      "node_id": "node_fourier_transform",
      "label": "傅里叶变换",
      "type": "concept",
      "summary": "将信号从时域表示转换为频域表示的数学工具",
      "level": 0,
      "importance": 0.92,
      "source_refs": [
        {
          "type": "segment",
          "id": "seg_001",
          "ts": 1.0
        }
      ]
    }
  ],
  "edges": [
    {
      "edge_id": "edge_node_fourier_transform_maps_to_node_frequency_domain",
      "source": "node_fourier_transform",
      "target": "node_frequency_domain",
      "relation": "maps_to",
      "source_refs": [
        {
          "type": "segment",
          "id": "seg_001",
          "ts": 1.0
        }
      ]
    }
  ],
  "updated_at": "2026-06-05T01:02:03.000000+00:00"
}
```

前端实时展示时优先消费 WebSocket 的 `graph_patch`；课后/历史展示时读取
完整 `knowledge_graph.json`。

## 9. 本地保存文件

结束课堂后，后端写入：

```text
data/sessions/{session_id}/metadata.json
data/sessions/{session_id}/transcript.md
data/sessions/{session_id}/structured_notes.md
data/sessions/{session_id}/timeline.json
data/sessions/{session_id}/knowledge_graph.json
data/sessions/{session_id}/post_class_status.json
data/sessions/{session_id}/summary.md
data/sessions/{session_id}/todos.json
data/sessions/{session_id}/quiz.json
data/sessions/{session_id}/agent_messages.json
data/sessions/{session_id}/agent_artifacts.json
data/sessions/{session_id}/images/
```

文件说明：

| 文件 | 内容 |
| --- | --- |
| `metadata.json` | `LectureSession` |
| `transcript.md` | 人可读 Markdown 字幕 |
| `structured_notes.md` | WhisperLive/Qwen 链路实时维护的结构化课堂笔记，可能不存在 |
| `timeline.json` | `TimelineItem[]` |
| `knowledge_graph.json` | `KnowledgeTree` |
| `post_class_status.json` | 课后后台生成状态、阶段、步骤耗时、warnings 和最终图谱状态 |
| `summary.md` | 课后总结，结束课堂后由后台任务生成 |
| `todos.json` | 课后待办候选，结束课堂后由后台任务生成 |
| `quiz.json` | 用户主动通过 Agent 生成自测题后保存 |
| `agent_messages.json` | 历史 Agent 对话 |
| `agent_artifacts.json` | Agent 生成的结构化产物快照 |
| `images/` | 前端或硬件摄像头上传的课堂图片 |

## 10. Mock Sender

mock sender 用于在真实 ASR、摄像头/视觉分析和内部知识抽取模块未完全接入时，
自动向后端喂一组中文课堂模拟数据。它不会创建课堂；课堂开始必须先从
前端页面手动发起。

先启动后端：

```bash
.venv/bin/uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

先在前端点击开始课堂，复制页面上的 `session_id`，然后运行：

```bash
.venv/bin/python backend/scripts/mock_sender.py --session-id REPLACE_WITH_SESSION_ID --no-end
```

快速发送：

```bash
.venv/bin/python backend/scripts/mock_sender.py --session-id REPLACE_WITH_SESSION_ID --delay 0 --no-end
```

发送完并触发结束课堂：

```bash
.venv/bin/python backend/scripts/mock_sender.py --session-id REPLACE_WITH_SESSION_ID
```

指定后端地址：

```bash
.venv/bin/python backend/scripts/mock_sender.py --base-url http://127.0.0.1:8000 --session-id REPLACE_WITH_SESSION_ID --no-end
```

mock sender 当前会模拟：

1. 使用已有课堂 session。
2. 发送多条 `transcript.segment`。
3. 发送 `image.capture`，包含可选 OCR 文本和多模态描述。
4. 发送多条模拟的内部 `knowledge.extraction`，驱动知识图谱增量更新。
5. 默认结束课堂并保存本地文件；加 `--no-end` 时保留 recording 状态。

## 11. 本地音频与 WhisperLive 联调脚本

### audio-stream

`audio-stream` 是 OpenVINO Whisper/Qwen 本地测试链路，适合不启动
WhisperLive 服务时快速验证：

```bash
scripts/dev.sh audio-stream --max-audio-seconds 120 --whisper-device GPU --qwen-device CPU
```

默认会尝试自动接入 `GET /sessions/recording` 返回的最新录制课堂；如果没有
可用课堂，会自动调用 `POST /sessions/start` 创建测试课堂。也可以显式指定：

```bash
scripts/dev.sh audio-stream --session-id lec_xxx --max-audio-seconds 120
```

### whisperlive-md

`whisperlive-md` 是当前主要的本地课堂笔记联调链路：

```bash
scripts/dev.sh whisperlive-server --port 9090
scripts/dev.sh whisperlive-md --max-audio-seconds 300 --update-every-seconds 30
```

启用云端知识图谱更新：

```bash
scripts/dev.sh whisperlive-md \
  --enable-cloud-graph \
  --max-audio-seconds 300 \
  --update-every-seconds 30 \
  --graph-update-every-seconds 60
```

行为：

1. WhisperLive 生成字幕草稿。
2. 本地 Qwen 定期根据字幕草稿维护结构化 Markdown 课堂笔记。
3. 如果绑定了 session，Markdown 保存到
   `data/sessions/{session_id}/structured_notes.md`。
4. 启用 `--enable-cloud-graph` 后，脚本定期调用
   `POST /agent/knowledge-tree/update-from-notes`。
5. 后端 notes-agent 生成 `knowledge.extraction`，前端通过标准 `graph_patch`
   更新知识图谱。
6. final 快照如果返回 `session_title` / `course`，后端会更新课堂元信息并
   广播 `session.updated`。

### whisperlive-mic

`whisperlive-mic` 是真实麦克风采集入口，参考桌面采集项目的 ALSA/ffmpeg
方式自动选择 USB 麦克风，并把 16 kHz mono float32 PCM 直接送入
WhisperLive：

```bash
scripts/dev.sh whisperlive-server --port 9090
scripts/dev.sh whisperlive-mic --enable-cloud-graph
```

指定设备或只测试 ASR 字幕：

```bash
scripts/dev.sh whisperlive-mic --audio-device plughw:1,0 --language zh
scripts/dev.sh whisperlive-mic --audio-device plughw:1,0 --language en
scripts/dev.sh whisperlive-mic --no-qwen-notes --max-audio-seconds 60
```

行为：

1. 通过 `arecord -l` 自动选择 USB/mic 类 ALSA 输入设备；也可用
   `--audio-device` 显式指定。
2. 用 ffmpeg 从麦克风读取音频并重采样为 WhisperLive 需要的 16 kHz mono
   float32 PCM。
3. WhisperLive 返回 completed 字幕后，脚本发送标准
   `transcript.segment` 到 `/events`。
4. WhisperLive partial 字幕会通过 `POST /events/transcript-preview` 广播给
   前端作为一条可替换的“正在识别”临时字幕；该通道在课堂进行中不写入
   transcript、timeline、笔记、知识图谱或历史文件。点击结束课堂时，前端会
   把当前可见的最后一条有效 preview 调用
   `POST /events/transcript-preview/finalize` 补写为 final 字幕。
5. 默认语言为 `auto`；中文/英文课可以分别显式传 `--language zh` 或
   `--language en`，避免混合环境误判。
6. 默认会自动接入最新 recording 课堂；没有可用课堂时会创建本地测试课堂。
7. 未传 `--no-qwen-notes` 时，仍会定期维护
   `data/sessions/{session_id}/structured_notes.md`。
8. 传 `--enable-cloud-graph` 时，结构化笔记继续上传给 notes-agent 更新图谱。

调低体感字幕延迟时可先调整：

```bash
scripts/dev.sh whisperlive-mic \
  --no-qwen-notes \
  --partial-preview-interval 0.5 \
  --same-output-threshold 2
```

## 12. 前端接入建议

实时课堂页面建议流程：

1. 调用 `POST /sessions/start` 创建课堂。
2. 用返回的 `session_id` 连接 `WS /ws/{session_id}`。
3. 收到 `ws.connected` 后展示连接成功状态。
4. 收到 `event.received` 后：
   - 把 `context_update.timeline_item` 写入本地状态；当前主界面不再单独显示事件面板，
     但 timeline 仍用于历史保存和后续定位。
   - 如果 `event_type` 是 `transcript.segment`，更新字幕区。
   - 如果 `event_type` 是 `image.capture`，更新图片/视觉分析区。
   - 如果 `graph_patch` 不为 `null`，应用知识图谱增量更新。
5. 如果当前还有正在显示的 `transcript.preview`，先调用
   `POST /events/transcript-preview/finalize` 补写最后一条有效临时字幕。
6. 调用 `POST /sessions/{session_id}/end` 结束课堂。
7. 收到 `session.ended` 后停止实时写入，展示保存路径或进入课后页面。
8. 收到 `post_class.updated` 后刷新 summary/todos/RAG 状态和最终图谱信息。

注意：当前 WebSocket 连接后不会补发历史快照。因此前端应在创建课堂后尽快
连接 WebSocket，再开始发送或接收实时事件。
