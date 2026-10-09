# EDU-Mate LLM Provider Setup

首次安装和桌面启动步骤见 [部署指南](DEPLOYMENT_GUIDE.md)。本文只维护模型、
代理、超时和检索配置。以下供应商示例对应项目现有模板，模型权限、可用性和
图片能力需在自己的服务账号中确认，不应把示例名称理解为已通过全链路验收。

EDU-Mate uses one backend-only OpenAI-compatible LLM client for Agent skills,
internal LLM-backed knowledge extraction, the structured-notes knowledge tree
agent, and optional classroom image analysis. API keys must stay on the backend;
never expose them in the frontend bundle or browser requests.

This cloud/local OpenAI-compatible provider is separate from the local OpenVINO
Qwen used by `scripts/dev.sh whisperlive-md`. Local Qwen maintains
`structured_notes.md`; the backend provider turns those notes into graph updates
and, on final snapshots, can infer the classroom title and course.

## Shared Environment Variables

First-time users can run the local configuration wizard:

```bash
scripts/dev.sh llm-config
```

The wizard writes the same backend-only variables to `.env`. Advanced users can
still configure them directly in `.env` or export them before launching the
app. Direct environment variables take precedence over `.env`.

```bash
LLM_PROVIDER=deepseek
LLM_API_KEY=replace_me
LLM_MODEL=deepseek-v4-flash
LLM_BASE_URL=https://api.deepseek.com
LLM_TIMEOUT_SECONDS=60
LLM_MAX_RETRIES=1
LLM_IGNORE_PROXY=1
NO_PROXY=localhost,127.0.0.1
no_proxy=localhost,127.0.0.1
```

Local development commands in `scripts/dev.sh` load `.env` automatically for
backend, dev, audio/WhisperLive integration, LLM smoke, global-index rebuild,
app, and build commands. Put local secrets in `.env`; keep `.env` ignored by
Git and commit only `.env.example`.

For a one-command local app-style run:

```bash
scripts/dev.sh app
```

`app` mode runs the first-run LLM check, builds the frontend when build output
is missing or source files are newer, starts the backend without reload, and
serves the frontend on `FRONTEND_PREVIEW_PORT` (default `4173`). It also starts
WhisperLive and microphone capture by default when the OpenVINO environment is
available. `dev` starts only the backend and frontend.

Knowledge extraction is LLM-only. If the provider is not configured, EDU-Mate
will keep transcript, image capture, and classroom saving working, but it will return an
extraction error and skip graph generation.

The same provider is used by:

- `/agent/chat` LLM-backed skills and grounded QA.
- Internal realtime/end-of-session knowledge extraction.
- `/agent/knowledge-tree/update-from-notes`, which turns Qwen structured notes
  into knowledge-tree updates and final session metadata.
- `/agent/visual/analyze`, which sends a saved classroom photo to a multimodal
  model and turns the result into visual notes plus optional graph updates.

For image analysis, the configured model must support OpenAI-compatible
multimodal chat content. A text-only model can still power QA and graph
extraction, but `/agent/visual/analyze` will fail with a warning.

Cloud LLM requests default to direct connections and ignore inherited
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` variables. This avoids failures when
a desktop proxy app is closed but the EDU-Mate process still inherits stale
proxy ports. Set `LLM_IGNORE_PROXY=0` only when your cloud provider must be
accessed through the system proxy. `NO_PROXY` / `no_proxy` are still kept for
local backend/frontend addresses.

Image analysis has an extra retry-friendly timeout behavior: the first request
uses `LLM_TIMEOUT_SECONDS`; if the same image fails and the recording frontend
retries with `force=true`, the visual analysis agent increases the timeout for
that image up to a capped value. This avoids blocking the initial photo capture
while still giving slow multimodal calls more room on retry.

Post-class generation also has stage-specific cloud timeouts so one slow cloud
step does not hide completed local notes or other artifacts:

```bash
POST_CLASS_GRAPH_LLM_TIMEOUT_SECONDS=45
POST_CLASS_GRAPH_LLM_MAX_RETRIES=0
POST_CLASS_ARTIFACT_LLM_TIMEOUT_SECONDS=45
POST_CLASS_ARTIFACT_LLM_MAX_RETRIES=0
```

The local Qwen final notes stage is controlled separately. With current defaults,
the backend may first wait up to `POST_CLASS_FINAL_NOTES_WAIT_SECONDS=240` for
the microphone worker's final notes, then launch a Qwen subprocess with
`POST_CLASS_QWEN_NOTES_TIMEOUT_SECONDS=360` if no usable final notes exist.
These are separate waits; a cloud timeout does not bound the entire post-class
pipeline. Once notes are ready, graph and artifacts run independently, while
summary and todos still run sequentially inside the artifacts job.

## Vector RAG / LlamaIndex

The default RAG backend is still lexical search because it has no external
model dependency. To enable vector retrieval for classroom Agent QA and
cross-classroom search, install the optional dependencies:

```bash
scripts/dev.sh install-rag
```

On the current device, this command installs CPU-only torch before installing
the rest of the RAG stack. Set `RAG_INSTALL_CPU_TORCH=0` only if you have a
separately managed torch build and want pip to reuse it.

Then configure `.env`:

```bash
RAG_QUERY_BACKEND=llamaindex
GLOBAL_SEARCH_BACKEND=llamaindex
RAG_EMBEDDING_BACKEND=huggingface
RAG_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RAG_EMBEDDING_DEVICE=cpu
RAG_LLAMAINDEX_LLM=disabled
```

The first HuggingFace embedding run may download the embedding model and can be
noticeably slower. Keep `RAG_EMBEDDING_DEVICE=cpu` on the current device unless
you have separately validated GPU/NPU embedding support.

`RAG_LLAMAINDEX_LLM=disabled` is intentional. EDU-Mate uses LlamaIndex as the
vector retriever, then builds source-grounded answers through its own Agent
logic so source constraints remain consistent.

Useful checks:

```bash
scripts/dev.sh rag-smoke --require-llamaindex
scripts/dev.sh rebuild-global-index --llamaindex
```

The global index writes an auditable snapshot to
`data/indexes/global/documents.json`, a manifest to
`data/indexes/global/manifest.json`, and the persisted vector index to
`data/indexes/global/llama_index/`. Search lazily rebuilds the vector index when
the manifest fingerprint changes; the CLI command above forces a rebuild before
demos.

If optional dependencies, embedding model download, or index loading fails,
EDU-Mate returns a warning and falls back to lexical search instead of failing
the API request.

## DeepSeek

The current project template uses `deepseek-v4-flash` and offers
`deepseek-v4-pro` as an alternative. The backend appends `/chat/completions`
to the configured base URL. Confirm model access with your provider account.

```bash
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-v4-flash
LLM_BASE_URL=https://api.deepseek.com
```

Alternative template:

```bash
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-v4-pro
LLM_BASE_URL=https://api.deepseek.com
```

Model names are configuration, not a restriction imposed by the backend. Use
the exact model identifier supported by your endpoint.

## Kimi / Moonshot

Kimi uses an OpenAI-compatible Chat Completions API. The Chinese API endpoint is:

```bash
LLM_PROVIDER=kimi
LLM_API_KEY=sk-...
LLM_MODEL=kimi-k2.6
LLM_BASE_URL=https://api.moonshot.cn/v1
```

The client normalizes requests identified as Kimi/Moonshot to `temperature=1`.
For image analysis, verify that the selected model and endpoint accept
`image_url` content; a successful text smoke test does not verify image support.

## Qwen / Other Compatible Providers

For an OpenAI-compatible multimodal endpoint, configure:

```bash
LLM_PROVIDER=openai_compatible
LLM_API_KEY=replace_me
LLM_MODEL=replace_with_your_vision_model
LLM_BASE_URL=https://your-provider.example/compatible-mode/v1
```

Replace both placeholders with the model and endpoint assigned by your provider.
Private deployment URLs and real API keys belong only in `.env`. This changes
the shared backend provider, including graph and artifact requests; it does not
replace the local OpenVINO Qwen model that generates classroom notes.

## OpenAI

```bash
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=https://api.openai.com/v1
```

## Local OpenAI-Compatible Provider

For Ollama, vLLM, llama.cpp server, or another local OpenAI-compatible endpoint:

```bash
LLM_PROVIDER=local
LLM_API_KEY=
LLM_MODEL=llama3.1
LLM_BASE_URL=http://127.0.0.1:11434/v1
```

`local` is allowed to run without `LLM_API_KEY`; cloud providers require a key.

## Smoke Test

```bash
scripts/dev.sh llm-smoke
```

This tests configured cloud text calls, not microphone capture, local Qwen or
image analysis. Follow the acceptance steps in the [deployment guide](DEPLOYMENT_GUIDE.md#8-怎样判断部署成功)
for the full classroom flow. Detailed event formats live in [API_SCHEMA.md](API_SCHEMA.md).

## Failure Behavior

- Missing provider config returns `ExtractionError`.
- Provider timeout or HTTP failure returns `ExtractionError`.
- Invalid JSON/schema output returns `ExtractionError`.
- Multimodal image analysis without a vision-capable model returns
  `status="failed"` and marks the image as failed.
- EDU-Mate does not fall back to the legacy rule extractor.
- Invalid extraction output is not written to the graph.
- Notes-agent failures return `status="failed"` and warnings; subtitles and
  structured Markdown saving continue.
