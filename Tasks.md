# EDU-Mate Active Tasks

本文件只保留后续开发 checklist。项目总览见 `AGENTS.md`，接口契约见 `docs/`。

## Done Baseline

- [x] FastAPI backend and React/Vite frontend.
- [x] Session lifecycle, realtime events, WebSocket updates, local history.
- [x] Transcript, classroom image/visual analysis, knowledge graph, post-class artifacts, Agent, and
  global search panels.
- [x] History list/detail/delete.
- [x] Agent chat: QA / summary / todos / quiz.
- [x] Strict and grounded QA modes.
- [x] Post-class `summary.md` and `todos.json`; on-demand `quiz.json`.
- [x] Persist post-class generation status and surface interrupted generation as failed.
- [x] Agent messages and artifacts persistence.
- [x] Lexical RAG, optional single-classroom LlamaIndex, optional global index.
- [x] Cloud/local OpenAI-compatible LLM provider support.
- [x] Internal LLM-backed knowledge extraction during recording and at session end.
- [x] WhisperLive server wrapper and WhisperLive/Qwen Markdown pipeline.
- [x] ALSA microphone capture path streams live audio to WhisperLive and the
  existing transcript/notes/graph workflow.
- [x] WhisperLive microphone partials can show as frontend-only preview subtitles;
  only final subtitles are persisted.
- [x] Ending a classroom finalizes the currently visible useful preview subtitle
  before saving, so the last accepted partial is not lost.
- [x] Local Qwen generates structured classroom notes without rewriting realtime subtitles.
- [x] Cloud notes-agent can infer final classroom title/course from notes.
- [x] Notes-agent endpoint for Markdown-driven graph updates.
- [x] Post-class generation is staged: final Qwen notes publish first, summary/todos
  artifacts and final graph update run independently, and each stage records status
  and timing.
- [x] Auto attach/create session support for local audio scripts.
- [x] Compact Agent source refs; no full subtitle dump in source display.
- [x] Strict QA can use LLM for natural source-only answers with fallback.
- [x] Strict QA validates LLM answers against retrieved classroom sources.
- [x] Notes-agent/graph layer filters low-value items and merges duplicate labels.
- [x] Notes-agent skips repeated graph content across Markdown snapshots.
- [x] Course aggregation, merged course knowledge tree, and cross-classroom review QA APIs.
- [x] Cross-classroom UI exposes search/review mode and course quick filters.
- [x] Image upload/read endpoints.
- [x] Browser camera preview and photo capture, with button and Ctrl+1 shortcut.
- [x] Cloud multimodal image analysis writes visual text/key points into the
  existing visual panel, knowledge graph, and RAG source flow.
- [x] Failed classroom image analysis is written back as `status=failed`, shown
  to the user, and retried automatically while the class is still recording.
- [x] Knowledge graph graph-view supports pan/zoom/fullscreen, complete multiline labels,
  and relationship-cluster layout.
- [x] App mode can launch backend, built frontend preview, WhisperLive server,
  and microphone capture from one command or desktop shortcut.
- [x] Deleting a saved classroom cancels/guards post-class background writes so
  deleted session directories are not recreated.

## P0: Integration Reliability

- [ ] Run a full WhisperLive/Qwen/notes-agent classroom test with real provider.
- [ ] Confirm iGPU WhisperLive model choice and latency target on device.
- [ ] Confirm Qwen CPU structured-notes cadence that does not block ASR.
- [ ] Confirm cloud graph update cadence and stage-specific timeout settings.
- [ ] Verify frontend can attach to script-created recording sessions reliably on the target device.
- [ ] Add a concise full-chain manual test record with observed timings.
- [ ] Validate browser camera permission/device behavior on the target board.
- [ ] Validate ALSA microphone selection, gain, and WhisperLive latency on the
  target board.
- [ ] Decide whether hardware camera capture should call the same image upload +
  visual analysis endpoint or provide its own capture service wrapper.
- [ ] Validate end-session preview finalization on long microphone sessions with
  noisy partial ASR.

## P1: Agent And Knowledge Quality

- [ ] Add deeper evaluation for notes-agent graph deduplication and hierarchy stability.
- [ ] Add fixtures for long 6-8 minute classroom graph growth.
- [ ] Tune RAG ranking across structured notes, graph nodes, visual sources, and subtitles.
- [ ] Support clicking Agent/review source refs to focus subtitle/image/graph node.
- [ ] Add frontend tests for URL deep link and focused source behavior.
- [ ] Add clearer UI display for LLM warnings and graph update failures.

## P1: Provider, RAG, And Index Docs

- [x] Document `RAG_QUERY_BACKEND=llamaindex`.
- [x] Document `GLOBAL_SEARCH_BACKEND=llamaindex`.
- [x] Document embedding provider options.
- [x] Add LlamaIndex dependency/install notes.
- [x] Add global index manifest/check/rebuild notes.
- [x] Add warning behavior examples for provider/index fallback.

## P2: Productization

- [ ] Export full classroom Markdown package.
- [ ] Export knowledge graph as Mermaid.
- [ ] Export todos as ICS.
- [ ] Export quiz to Anki-compatible format.
- [ ] Add provider/API key configuration UI.
- [ ] Add model/service status panel.
- [ ] Add log viewer for ASR/Qwen/cloud graph updates.
- [ ] Write Ubuntu/DK deployment and startup service docs.

## Validation Commands

```bash
scripts/dev.sh compile
scripts/dev.sh backend-test
scripts/dev.sh test
scripts/dev.sh build
```

Local integration:

```bash
scripts/dev.sh dev
scripts/dev.sh whisperlive-server --port 9090
scripts/dev.sh whisperlive-md --enable-cloud-graph --max-audio-seconds 300
```

LAN integration:

```bash
BACKEND_HOST=0.0.0.0 FRONTEND_HOST=0.0.0.0 scripts/dev.sh dev
```
