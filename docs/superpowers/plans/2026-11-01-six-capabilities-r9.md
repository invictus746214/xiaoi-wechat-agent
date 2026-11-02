# Six capabilities r9 implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Track each task through failing tests, implementation, regression and review.

**Goal:** Deliver the six user-approved improvements without weakening source verification, privacy or delivery receipt semantics.

**Architecture:** Small capability modules integrate at job preparation and existing task/command routing. Existing SQLite state and the unique GUI executor remain authoritative.

**Tech Stack:** Python, SQLite, unittest, Linux WeChat / X11; bounded optional speech decoding and recognition.

**Spec:** `docs/SIX_CAPABILITIES_R9.md`.

## Global constraints

- Do not send messages or export real chat/media as tests; real user acceptance is recorded separately.
- Account/person/scene scope all corrections, follow-ups and commands.
- Never replay sending/unconfirmed native actions.
- Waiting is bounded and never sleeps in the main executor.
- Owner management is explicit and private; default settings remain safe.

## Review focus

- Missing native audio or ASR dependency must return an explicit unavailable state, never invented speech.
- A group member cannot correct another member's media or alter runtime settings.
- Coalescing must preserve existing draft/retry boundaries and allow other chats to progress.
- Restart and duplicate events cannot schedule duplicate follow-ups or repeat controls.
- Failed or uncertain external actions retain their existing receipt state.

## Task 1 — Incoming voice

- [x] Add verified voice extraction/transcription in `voice_input.py` and necessary reader methods.
- [x] Add failing tests, implement bounded input validation and unavailable paths, then pass focused tests.
- [x] Integrate real transcripts into existing job context; verify resource limits and deployment requirements.

## Task 2 — Sticker management

- [x] Add `sticker_commands.py`, scoped progress and controlled tag corrections.
- [x] Test real SQLite/library state across scenes/persons and unknown native outcomes.
- [x] Route commands before model generation without bypassing privacy or native confirmation.

## Task 3 — Group context and pacing

- [x] Improve group focus/turn handling in `conversation_memory.py`.
- [x] Extend `Inbox.claim` with optional bounded quiet/coalescing windows.
- [x] Test @, quotes, other recipients, busy chats, max wait and draft boundaries; wire configuration.

## Task 4 — Consented follow-up

- [x] Add explicit consent-based follow-up handling using `Tasks` delivery.
- [x] Test creation, duplicate events, current-scene scope, cancellation and expired timing.

## Task 5 — Owner controls

- [x] Add persistent typed runtime overrides for verified owner private commands.
- [x] Test non-owner rejection, expiry, restart, replay and effective configuration.
- [x] Integrate before normal command/model routing and periodic configuration reload.

## Task 6 — Release

- [x] Run full regression, private-data scan, isolated Linux checks and scoped real-data read checks.
- [x] Deploy with hash preconditions, backups and rollback; verify account-ready executor.
- [x] Prepare the checked public source update and record completed and outstanding real acceptance.
