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

## 2026-04-22 — PDF download works, fleet dates fixed (IST)

**What changed**
- **PDF downloads fixed.** When the assistant generates a PDF, the chat
  response now shows the **full download URL as plain text**
  (e.g. `https://ai.naarni.com/api/chat/file/<id>`). Users can copy-paste
  the URL into a browser tab to download. The previous clickable
  markdown link often did nothing when clicked; the plain URL is a
  reliable fallback.
- **Fleet date/time handling corrected.** Fleet questions ("yesterday",
  "last week", specific dates) now match what the dashboard web app
  sends — plain IST wall-clock strings, read literally by the Naarni
  backend. Previously the tool was anchored in UTC for "today" (caused
  off-by-one-day errors near midnight IST), and a morning attempt to
  fix it over-corrected by converting IST→UTC (caused a totally wrong
  time window on every query). Now matches the web app exactly.
- **Default fleet query window is now the last 7 days** (was 30).
  Matches what the dashboard UI defaults to and gives the assistant
  more focused, recent data.

**Why**
- Broken download links and wrong-date answers were the top two
  complaints from internal testers this week.

**Known issues / follow-ups**
- A belt-and-suspenders frontend change (always-visible Download chip,
  independent of the LLM text) is queued for a later release.

**Behind the scenes**
- S3 credentials for the document store (`naarni-onyx-docs-*`) were
  rotated. Production was briefly in a crash-loop during the key
  change; traffic was restored within ~15 minutes.
- CI deploy pipeline was failing with two separate git-submodule
  errors (`.claude/worktrees/...` and `analytics-service`). Both
  directories had been committed as gitlinks with no `.gitmodules`
  entry. Removed the stray entries, corrected a `.gitignore` typo
  (`anayltics-service` → `analytics-service`), and added `.claude/`
  to the ignore list so the mistake can't recur.
- Pre-flight script and ops doc for scheduled VM resize (E8s_v5 ↔
  E4s_v5, Mon–Fri working hours) landed as internal tooling. The
  automation itself is not yet live — awaiting ops sign-off.
- Three production deploys today: initial PDF plain-URL + IST fix,
  mid-day CI unblock, and the late-afternoon fleet-date correction.

---

## 2026-04-21 — First-pass PDF download + VM resize groundwork

**What changed**
- First attempt at fixing the PDF download flow: the assistant now
  hands the LLM a pre-formatted markdown link with the correct URL
  instead of letting it construct the URL from scratch.
- Internal tooling: pre-flight verification script and operations
  guide for the upcoming scheduled VM resize (cost savings in
  off-hours). No user-visible effect yet.

**Known issues / follow-ups**
- Clicking the markdown download link still failed for many users.
  Superseded by the 2026-04-22 plain-URL fix.

---

## 2026-04-12 — Fleet data quality and LLM accuracy improvements

**What changed**
- Fleet questions now auto-group correctly. If you ask about a
  specific route or vehicle, the assistant breaks results down by
  that route/vehicle instead of returning fleet-wide aggregates.
- Route names ("Delhi to Dehradun") and vehicle registrations
  ("HR55AY7626") are auto-resolved to internal IDs — you no longer
  have to know the numeric ID.
- Vehicle records now include odometer reading, live GPS, AC status,
  month-to-date kilometers, and route cities. The assistant can answer
  questions like "where is bus HR55AY7626 right now?".
- Hour-level time granularity added (for within-day analytics).
- Fleet date inputs now tolerate millisecond suffixes (the Naarni
  backend was rejecting them).
- Built-in decision tree helps the LLM pick the right fleet endpoint
  for each question type (dashboard vs. performance vs. activity).

**Why**
- Internal testers were getting fleet-wide numbers when they asked
  about a single route, and were unable to query by human-readable
  names. This release closes both gaps.

---

## 2026-04-11 — Fleet responses reformatted for accuracy

**What changed**
- Performance, activity, and dashboard responses are now flattened
  and humanized before the assistant sees them. Unit suffixes
  (km, kWh, %, etc.) are embedded in field names so the assistant
  doesn't mislabel values.
- Fixed a bug where fleet metrics showed `0.0` due to a nested-array
  parsing issue in the upstream response.
- Vehicle-analytics responses are now denormalized — route and depot
  names are inlined so the assistant doesn't have to cross-reference
  tables.
- Naarni SMS OTPs are now correctly treated as 4 digits (was 6).
- First attempt at a Download button for generated PDFs (see
  2026-04-21 and 2026-04-22 for follow-ups).

**Why**
- Several users reported "0.0" readings on vehicles that had clearly
  been running. Root cause was response shape, not missing data.

---

## 2026-04-10 — PDF generation tool + Naarni Connect flow

**What changed**
- **Generate PDF reports from chat.** The assistant can now produce
  branded PDF reports (report + brief templates) on request —
  e.g. "generate a PDF of this week's fleet summary". NaArNi
  branding, cover page, table of contents, tables, and callouts are
  supported.
- **Naarni Connect modal** prompts users to link their Naarni fleet
  account right after OIDC sign-in, so fleet queries work on first
  use.
- Automatic Naarni token refresh — users stay logged in to the
  fleet backend without re-entering OTPs constantly.

**Why**
- Stakeholder request: shareable reports from inside the chat. First
  building block toward branded customer-facing deliverables.

---

## 2026-04-09 — Fleet data access from chat + phone auth

**What changed**
- Introduced the **Fleet Data tool**. Users can now ask the assistant
  operational questions about the EV bus fleet (vehicles, routes,
  depots, alerts, performance, activity) and get live answers from
  the Naarni backend.
- Naarni phone-number authentication: users link their Naarni
  account to NaArNi Gyan with a phone + OTP flow (Settings →
  Connect Naarni).

**Why**
- This is the flagship NaArNi-specific capability — without fleet
  access the assistant is just a generic chat bot.

---

## 2026-03-31 — M365 email search corrections

**What changed**
- M365 (Outlook) federated email search returns results correctly
  again. A wiring issue was routing email searches through the
  generic federated connector path; now they use the dedicated
  M365 email API.
- Logo sizing and version-branding tweaks on the UI.

**Why**
- Email search was silently returning empty results for several
  testers.

---

## 2026-03-30 — NaArNi branding + M365 connector (UI)

**What changed**
- **All Onyx logos replaced with NaArNi branding** (3-green-hexagon
  logo, "One System Infinite Possibilities" tagline, brand colors).
  App is now visually NaArNi Gyan, not Onyx.
- **M365 (Outlook) federated connector** added to the UI. Users can
  connect their Microsoft 365 account and search their Outlook mail
  and OneDrive content from inside the assistant.
- UI polish: logo sizing, page animations, version numbering.

**Behind the scenes**
- Production environment configuration files committed (no secrets).
- Docker Hub token rotated; CI build context paths corrected.

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
