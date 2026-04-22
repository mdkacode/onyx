# NaArNi Gyan — Release Notes

Stakeholder-facing changelog for NaArNi Gyan (the AI assistant at
https://ai.naarni.com). One entry per production deploy, newest on top.

**Conventions**
- Dates are in IST (Asia/Kolkata).
- Each release has: what changed (user-visible), why, and anything
  the stakeholder needs to know (known issues, workarounds, follow-ups).
- If a change is infra-only (no user impact), it lives under
  "Behind the scenes".

---

## 2026-04-22 — PDF download link fix

**What changed**
- When the assistant generates a PDF, the chat response now shows the
  **full download URL as plain text** (e.g. `https://ai.naarni.com/api/chat/file/<id>`).
  Users can copy-paste the URL into a new browser tab to download.
- The previous markdown-link approach looked clickable but often did
  nothing when clicked, which blocked downloads entirely.

**Why**
- The LLM was occasionally producing broken `[Download](...)` markdown
  where the href was missing or malformed. A plain-text URL removes the
  dependency on link rendering.

**Known issues / follow-ups**
- The clickable markdown link is still attempted alongside the plain
  URL — if the LLM produces it correctly, clicking it works too.
- A belt-and-suspenders frontend change (always-visible Download chip,
  independent of the LLM text) is queued for a later release.

**Behind the scenes**
- S3 credentials for the document store (`naarni-onyx-docs-*`) were
  rotated. Production was briefly in a crash-loop during the key
  change; traffic was restored within ~15 minutes.
- CI deploy pipeline was failing with a git submodule error
  (`No url found for submodule path '.claude/worktrees/...'`) after a
  local editor worktree was accidentally committed as a gitlink.
  Removed the stray entries from the repo and added `.claude/` to
  `.gitignore` so the mistake can't recur. No user impact.

---

## Template for future entries

> Copy this block to the top of the file for the next release.

```
## YYYY-MM-DD — <one-line headline>

**What changed**
- <bullet per user-visible change>

**Why**
- <1–2 sentences on motivation>

**Known issues / follow-ups**
- <anything the user should be aware of, or None>

**Behind the scenes**
- <infra/dev-only items, or omit section if none>
```
