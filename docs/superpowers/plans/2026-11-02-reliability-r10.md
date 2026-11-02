# Reliability r10 Implementation Plan

> Use superpowers:subagent-driven-development or superpowers:executing-plans; tests precede non-trivial implementation.

**Goal:** Make the existing assistant respond faster and avoid obsolete or unverifiable actions.
**Architecture:** Reuse the durable inbox/outbox, verified native sources, scoped followups and unique GUI executor. Add bounded progress and diagnostic metadata only where needed.
**Tech Stack:** Python, SQLite, unittest, existing local Vosk/pilk and Linux UI helpers.
**Spec:** `docs/RELIABILITY_R10.md`.

## Global constraints

No private media/text/key exports, no unknown action replay, no loss of pending input, and no change to existing owner/relay authorization or 20/20 Moments quotas.

## Review focus

- Interrupted migration must retry safely without falsely marking completion.
- New information must replace unsent stale drafts while preserving source and delivery boundaries.
- ASR caches/progress must invalidate on source/model/account changes.
- UI history scanning must yield and reject ambiguous artwork or uncertain outcomes.
- Followup changes must remain scoped and preserve unresolved sends; diagnostic reports expose only allowed metadata.

## Tasks

- [x] Test and implement account-scoped migration completion/empty fast path; measure real startup.
- [x] Measure voice phases; test and implement bounded optimizations with unchanged resource limits.
- [x] Add source-aware unsent reply supersession and tests for new info, other replies and uncertain deliveries.
- [x] Add bounded historical sticker navigation and durable progress; test exact matches, yielding, replay guards.
- [x] Add followup rescheduling/result handling and source-scoped consent/cancellation tests.
- [x] Add runtime/message diagnostics, route explicit queries and test privacy/timeout/offline states.
- [x] Run full regression, Linux/read-only real-data checks, deploy with rollback and verify readiness.
- [x] Prepare public documentation and checked source update; record frontend acceptance separately.

Evidence: Windows711 tests (3 platform skips); Linux711/no skips; account-verified production ready9.08s; 20/20 caps preserved. Frontend acceptance pending; historical batch native receipts19/36, not allcollected.
