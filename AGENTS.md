# EDU-Mate Development Guide

This file is the compact project guide. Keep detailed API contracts in `docs/`
and active roadmap items in `Tasks.md`.

## Current Shape

EDU-Mate is a local classroom Agent system:

- FastAPI backend manages classroom sessions, realtime events, WebSocket
  updates, local history, Agent APIs, RAG, and knowledge graph updates.
- React/Vite frontend shows realtime subtitles, classroom images and multimodal
  analysis, knowledge graph, post-class artifacts, classroom Agent, global
  search, and history.
- WhisperLive can produce streaming ASR drafts.
- Local Qwen turns WhisperLive subtitle drafts into periodically refreshed
  `structured_notes.md`; it no longer rewrites the realtime subtitle stream.
- A cloud LLM provider can power Agent skills, internal extraction, and
  notes-driven knowledge tree updates, including final classroom title/course
  inference from the structured notes.
- Historical review APIs support cross-classroom search, source-grounded review
  QA, course aggregation, and merged course knowledge trees.

## Capability Boundaries

External ASR/camera/hardware modules normally send only:

```text
transcript.segment
image.capture
```

`knowledge.extraction` is an EDU-Mate internal event. It may come from:

- internal LLM-backed extraction after ASR/image batches,
- notes-agent extraction from Qwen Markdown notes,
- mock/debug scripts.

If LLM extraction fails, EDU-Mate reports an explicit error and does not write
invalid graph updates. The legacy rule extractor is not a production fallback.

## Main Data Flows

Realtime classroom:

```text
POST /sessions/start
POST /events(transcript.segment/image.capture)
-> ContextManager
-> optional internal LLM extraction
-> KnowledgeGraphManager
-> WS /ws/{session_id}
```

WhisperLive microphone partials use a separate preview-only path:

```text
POST /events/transcript-preview
-> WS transcript.preview
-> one replaceable frontend "正在识别" subtitle row
```

Preview subtitles are not written to transcript, timeline, notes, graph, or
history while the lesson is live; completed/final subtitles still use
`transcript.segment`. When the user clicks "结束课堂", the frontend first
finalizes the currently visible useful preview via
`POST /events/transcript-preview/finalize`, so the last accepted partial is not
lost in `transcript.md`.

WhisperLive/Qwen notes:

```text
local audio file or ALSA microphone
-> WhisperLive ASR draft
-> local Qwen structured notes
-> data/sessions/{session_id}/structured_notes.md
-> POST /agent/knowledge-tree/update-from-notes
-> cloud LLM graph extraction and optional final title/course update
-> graph_patch/session.updated WebSocket update
```

Classroom images:

```text
Frontend camera preview + capture button / Ctrl+1
-> PUT /sessions/{session_id}/images/{image_id}
-> POST /events(image.capture status=processing)
-> ContextManager visuals + timeline
-> POST /agent/visual/analyze
-> cloud multimodal LLM returns caption/visual_text/key_points and graph items
-> image.capture + knowledge.extraction WebSocket updates
```

External camera or screenshot modules can still use the same image upload and
`image.capture` event contract. OCR is optional; the built-in classroom camera
path sends the saved image directly to a multimodal cloud LLM. Failed image
analysis is written back as `status=failed`; the recording frontend retries
with `force=true` and the backend can extend timeout based on prior failures.

History and Agent:

```text
POST /sessions/{session_id}/end
-> frontend finalizes the current preview subtitle if one is visible
-> data/sessions/{session_id}/...
-> session.ended returns quickly with post_class_status=generating
-> background local Qwen final structured notes
-> post_class.updated stage=notes_ready
-> summary/todos/title/index artifacts and final notes graph update run as
   separate background stages
-> post_class.updated stage=artifacts_ready / graph_ready / done
-> post_class_status.json records stage, steps, ready/failed, and graph finality;
   interrupted in-process work is
   surfaced as failed on next history read
-> POST /agent/chat
-> POST /agent/search
-> POST /agent/review
-> GET /agent/courses
```

## Common Commands

Use the helper first:

```bash
scripts/dev.sh help
```

Frequent commands:

```bash
scripts/dev.sh app
scripts/dev.sh desktop-shortcut
scripts/dev.sh dev
scripts/dev.sh test
scripts/dev.sh backend-test
scripts/dev.sh build
scripts/dev.sh llm-smoke
scripts/dev.sh rebuild-global-index
```

WhisperLive/Qwen integration:

```bash
scripts/dev.sh install-whisperlive
scripts/dev.sh whisperlive-server --port 9090
scripts/dev.sh whisperlive-md --max-audio-seconds 300 --update-every-seconds 30
scripts/dev.sh whisperlive-md --enable-cloud-graph --max-audio-seconds 300
scripts/dev.sh whisperlive-mic --enable-cloud-graph
scripts/dev.sh whisperlive-mic --audio-device plughw:1,0 --no-qwen-notes
```

Local audio smoke:

```bash
scripts/dev.sh audio-stream --max-audio-seconds 120 --whisper-device GPU --qwen-device CPU
```

LAN testing:

```bash
BACKEND_HOST=0.0.0.0 FRONTEND_HOST=0.0.0.0 scripts/dev.sh dev
```

## Backend Map

```text
backend/app/api/        HTTP/WebSocket routes
backend/app/core/       session/context/graph/websocket managers
backend/app/models/     Pydantic contracts
backend/app/storage/    local persistence and history
backend/app/agent/      Agent orchestration, global search, notes graph agent
backend/app/skills/     QA, summary, todos, quiz
backend/app/rag/        document conversion and lexical/LlamaIndex search
backend/app/llm/        OpenAI-compatible provider client
backend/app/extraction/ internal knowledge extraction
backend/scripts/        local integration utilities
backend/tests/          unittest suite
```

Layer rules:

- `api/` should stay thin: parse requests, call services/managers, map domain
  exceptions to HTTP.
- `core/` owns runtime state and graph updates.
- `agent/`, `skills/`, `rag/`, and `extraction/` should not depend on frontend
  concerns.
- Tests should use fake clients/services instead of real network or real LLM
  providers.

## Frontend Map

```text
frontend/src/App.tsx
frontend/src/components/
frontend/src/services/
frontend/src/stores/
frontend/src/types/
frontend/src/utils/
frontend/src/styles.css
```

Frontend rules:

- HTTP calls live in `services/`.
- WebSocket parsing lives in `services/websocket.ts`.
- Realtime merge logic lives in `stores/classroomStore.ts`.
- Components render state and trigger services; they should not own protocol
  parsing.
- TypeScript API types should mirror `docs/API_SCHEMA.md`.
- The graph view uses a lightweight SVG relationship-cluster layout: tightly
  connected nodes are grouped, local edges are emphasized, bridge edges are
  de-emphasized, and pan/zoom/fullscreen remain frontend-only concerns.

## Runtime Data

Ended sessions are stored under:

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
data/sessions/{session_id}/llama_index/
data/sessions/{session_id}/images/
```

Global search/index artifacts:

```text
data/indexes/global/documents.json
data/indexes/global/manifest.json
data/indexes/global/llama_index/
```

`data/sessions/*` is ignored by Git except `.gitkeep`. Do not destructively
clean local classroom data unless explicitly requested.

## Manual Smoke Flow

1. Start dev servers:

```bash
scripts/dev.sh dev
```

2. Open:

```text
http://127.0.0.1:5173
```

3. Start or attach to a classroom.
4. Run one integration source:

```bash
scripts/dev.sh mock --session-id REPLACE_WITH_SESSION_ID --no-end
# or
scripts/dev.sh whisperlive-md --enable-cloud-graph --max-audio-seconds 300
# or, in app mode, speak into the configured microphone after starting class
```

5. Verify subtitles, classroom images/analysis, graph updates, structured
   notes, Agent QA, post-class artifacts, and saved history.

## Documentation Map

- `AGENTS.md`: compact project guide.
- `Tasks.md`: active roadmap and checklist.
- `docs/API_SCHEMA.md`: HTTP/WebSocket/Agent API schema and ASR/camera/hardware input contract.
- `docs/LLM_PROVIDER_SETUP.md`: Kimi/DeepSeek/OpenAI/local provider setup.
- `docs/PACKAGING_PLAN.md`: app launcher, desktop shortcut, and packaging plan.
