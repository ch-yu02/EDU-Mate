# EDU-Mate Packaging Plan

This project is moving from a development-only workflow toward a directly
launchable classroom assistant. The frontend remains the only user-facing
source of start/end classroom actions; unfinished backend work continues in the
background after the user clicks end.

## Phase 1: Local App Entry

Implemented:

- `scripts/dev.sh llm-config` provides a first-run LLM provider wizard.
- `scripts/dev.sh app` checks LLM configuration, builds the frontend if needed,
  starts the backend without reload, and serves the built frontend with Vite
  preview.
- `scripts/dev.sh desktop-shortcut` installs/updates a desktop launcher that
  runs `scripts/launch_desktop_app.sh`.
- The desktop launcher starts app mode in a terminal and opens the browser to
  the local frontend automatically.
- App mode now starts the microphone stack by default:
  `WhisperLive server` plus `whisperlive-mic --wait-for-session
  --no-create-session`. The microphone waits for the frontend "start classroom"
  action before it posts transcript events, so the frontend remains the only
  source of classroom lifecycle intent.
- When the frontend ends a classroom, app-mode microphone capture stops writing
  to that ended session and re-arms itself for the next frontend-created
  classroom.
- `.env` remains supported for direct/manual configuration and takes precedence
  through exported environment variables.
- Provider templates are available for Kimi/Moonshot, DeepSeek, OpenAI,
  DashScope/Qwen, and local OpenAI-compatible services.

This phase is still a developer-friendly local runner, not a packaged binary.

## Phase 2: Desktop Shell

Planned:

- Replace the terminal-based desktop shortcut with a small desktop shell or
  native window that embeds the local frontend.
- Surface startup, API configuration, camera, microphone, WhisperLive, and
  background task status in the UI.
- Provide a first-run/reconfigure screen for provider templates while keeping
  `.env` loading for advanced users.
- Keep frontend start/end classroom actions as the only user-facing lifecycle
  source; microphone capture continues to wait for those actions.

## Phase 3: Runtime Supervision

Planned:

- Add process supervision for backend, frontend, WhisperLive, and microphone
  capture.
- Surface microphone/WhisperLive child-process failures in the frontend instead
  of only printing them in the launcher terminal.
- Add a small local status endpoint for readiness checks.
- Persist user-facing logs for startup failures, model/API failures, and device
  permission issues.
- Keep classroom end fast: save the session immediately, publish final Qwen
  notes as soon as they are ready, then finish summary/todos/title, final graph,
  quiz-on-demand, and optional index generation in background tasks.

## Phase 4: Distribution

Planned:

- Freeze backend dependencies and frontend build assets.
- Package the launcher and runtime assets for the target board.
- Add upgrade/migration handling for `.env`, session data, indexes, and logs.
- Add a factory-reset or reconfigure action for API credentials.
