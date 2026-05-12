# job-discovery — Design Spec

*Drafted 2026-05-12. Status: **shipped 2026-05-12** (live cron + Mizzix integration). Awaiting onboarding interview to populate `criteria.md`.*

A daily job-discovery system for Tavin, integrated into Mizzix (his Discord life-OS bot). Surfaces top-N matching engineering postings each morning, learns from his pass-reasons, and hands off to the existing `interview-coach` skill when a match is worth pursuing.

---

## Goals

- **Cast a wide net daily.** Pull from LinkedIn, Indeed, Glassdoor, Google Jobs, ZipRecruiter at 3am every day with no human in the loop.
- **Rank against living preferences.** Score each posting against Tavin's actual profile (career, storybank, prior pass-reasons), not a generic rubric.
- **Surface in the morning brief.** Top matches appear as a section in Mizzix's existing morning DM — same surface as calendar, todos, inbox.
- **Close the loop daily.** Evening check-in asks `applied / pass [reason] / tomorrow` for each surfaced match. Pass-reasons feed the next day's scoring.
- **Hand off cleanly.** When Tavin wants to act on a match (`decode #3`, `prep me for Westinghouse`), control passes to `interview-coach`. job-discovery owns discovery; interview-coach owns everything downstream.

## Non-goals

Explicitly NOT building (deferred to interview-coach or out of scope entirely):

- Resume tailoring (`interview-coach resume` covers it)
- Cover letter generation (interview-coach can produce drafts)
- Auto-applying to roles (system stays read-only on application forms)
- Recruiter outreach drafting (`interview-coach outreach` covers it)
- Salary research / comp benchmarking (`interview-coach salary` covers it)
- Job Tailor / Job Scout / Career-Ops / Role Scout (rejected upstream — see Decision Log)

---

## Architecture

Five components. New code in **bold**.

| # | Component | Where | Responsibility |
|---|---|---|---|
| 1 | **`job-discovery` skill** | `vault/skills/job-discovery/SKILL.md` | Pointer skill. Tells Mizzix when to invoke and which CLI commands exist. Mirrors the gig-finder pattern. |
| 2 | **`job_discovery` Python package** | `C:\Users\matis\Desktop\DevProjects\job-discovery\` (own repo, GitHub) | The actual code: JobSpy wrapper, scoring agent, state I/O, CLI. |
| 3 | **`MizzixJobDiscovery` cron** | Windows Task Scheduler, daily 3:00 AM | Runs the pipeline, writes results to `vault/.mizzix_state/job_matches.json`. |
| 4 | **Morning brief integration** | Edit Mizzix `heartbeat.py` morning brief renderer | Reads `job_matches.json`, renders the new section. |
| 5 | **EOD check-in trigger** | Edit Mizzix `heartbeat.py` heartbeat triggers | New deterministic trigger ~7pm if any matches are still un-actioned. DMs the list, parses reply, updates state. |

### Repo structure

```
job-discovery/
├── README.md
├── DESIGN.md                ← this file
├── pyproject.toml
├── job_discovery/
│   ├── __init__.py
│   ├── cli.py               ← entrypoints called by skill + cron
│   ├── search.py            ← JobSpy wrapper, per-board fetch + dedupe
│   ├── score.py             ← scoring agent (Claude Agent SDK) + rule-based fallback
│   ├── state.py             ← read/write job_matches.json + history + vault files
│   └── prompts/             ← scoring prompt templates
└── tests/
    ├── test_state.py
    ├── test_search.py
    ├── test_score.py        ← integration test against hand-labeled JD fixtures
    └── fixtures/
        └── jds/             ← real-shaped JDs with human-labeled scores

(EOD reply parsing is NOT a Python module — Mizzix parses replies conversationally
per the SKILL.md instructions, then calls `cli.py record-action` per parsed item.)
```

### Daily data flow

```
3:00 AM  cron fires `python -m job_discovery.cli scan`
         → JobSpy hits 5 boards with criteria.md filters
         → raw results dedupe vs. job_matches.json (surfaced)
                              + job_matches_history.json (resolved)
         → scoring agent scores each new candidate
              (reads: criteria.md, preferences.md, recent pass-reasons,
                       tavin.md career section, Job_Search README target profile)
         → top-N (default 5) merge into job_matches.json with status=surfaced
         → un-actioned entries from yesterday roll forward (still surfaced)

morning brief (existing heartbeat tick)
         → reads job_matches.json
         → renders "Job matches" section, sorted by overall score

~7:00 PM (heartbeat tick)
         → if any status=surfaced entries exist, EOD check-in fires
         → DMs the list, parses reply, mutates state
```

---

## File layout

### Vault — human-readable, in `projects/Job_Search/discovery/`

| File | Purpose | Edited by |
|---|---|---|
| `criteria.md` | Static search criteria — role types, geographies, salary floor, must-haves, dealbreakers, scoring weights. Single source of truth for what the daily scan is hunting. | Created by onboarding interview, edited by Tavin anytime |
| `preferences.md` | Append-only ledger of pass-reasons + a "Learned patterns" section at the top. Scoring agent reads this so future runs reflect evolving taste. | EOD check-in writes raw entries; weekly retro promotes recurring patterns |
| `applications.md` | Lightweight per-application ledger — date, company, role, source link, current status. Most apps die without response; this stays separate from `README.md → Active interview loops` (which tracks only live conversations). | EOD check-in writes "applied" rows; Tavin/Mizzix update status if recruiters respond |

### Machine state — in `vault/.mizzix_state/`

| File | Purpose |
|---|---|
| `job_matches.json` | Currently surfaced + un-actioned matches. Each entry: `id`, `source` (board), `title`, `company`, `location`, `salary`, `url`, `posted_date`, `surfaced_date`, `score` (overall + 6-dim breakdown), `one_line_take`, `status` (`surfaced` / `snoozed`), `last_brief_date`, `times_carried`, optional `decoded` flag, optional `score_method: "fallback"` flag. Surviving entries roll forward into tomorrow's brief if still un-actioned. |
| `job_matches_history.json` | Append-only archive of resolved matches (applied / passed / expired) with `action_date` and `pass_reason`. Used for: dedupe (don't re-surface a job Tavin already passed on), stats, and weekly retro pattern detection. Pruned by age (drop entries >90 days). Note: scoring does NOT read this file directly — it reads `preferences.md`, which holds the same pass-reasons in human-readable form. |
| `job_discovery.log` | Cron stdout/stderr. Useful for debugging board failures and scoring errors. |

### Vault skill pointer — `skills/job-discovery/SKILL.md`

Standard SKILL.md frontmatter (`name`, `description`) plus a body that tells Mizzix:
- Repo path: `C:\Users\matis\Desktop\DevProjects\job-discovery\`
- CLI commands available (see Skill Commands section below)
- Trigger phrases for each command
- Handoff rules to `interview-coach` (when Tavin asks something substantive about a specific match → invoke interview-coach `decode` on that JD URL)

---

## Scoring rubric

Mizzix-side scoring, modeled on Job Scout's 6-dimension rubric, anchored to Tavin's profile.

### Dimensions

Each scored 1–5.

| Dim | What it measures | Anchored to Tavin |
|---|---|---|
| **Role fit** | How well title + scope match what Tavin wants | Thermal/mech design = 5. Systems/test eng = 4. AI eng (founding/tools) = 4. Coordination-heavy / PM-ish = 1–2. |
| **Skills match** | Overlap between JD requirements and actual experience | Reads `tavin.md` career section + interview-coach storybank. |
| **Seniority** | Whether the level fits where Tavin is | Calibrated for early-mid IC. "Sr." with 5+ yrs required = 2–3. "Principal/Staff" = 1. New grad = 4. |
| **Domain** | Industry alignment | Aerospace = 5. Industrial/energy/HRSG-type = 4. Generic mfg = 3. Defense = TBD by onboarding interview. |
| **Location** | Geography fit | Chicago = 5. Milwaukee/Seattle/Denver = 4. Other medium-large city = 3. Small city = 2. LA = 1 (heavily downweighted but **not gated**; Scotia fallback exists for LA per Job_Search/README). |
| **Responsibilities** | Hands-on vs. coordination | Hands-on design/build = 5. Mixed = 3. Pure coordination = 1. |

### Hard gates

Auto-reject before scoring (status `gated`, never surfaces). Default list is **empty**; entries added only via onboarding interview or `update-criteria`.

Examples of gates Tavin might add later (none assumed at design time):
- Salary listed and below a hard floor
- Visa sponsorship required when N/A
- Specific dealbreaker companies/industries

### Combination logic

- **Overall score** = weighted average. Default weights: Role fit ×1.5, Domain ×1.5, others ×1.0. Configurable in `criteria.md`.
- **Surface threshold:** top N=5 with overall ≥ 3.0. If fewer than 5 clear that bar, surface fewer (don't pad with weak matches).
- **Tie-breaker:** most recently posted first.

### Pass-reason learning loop

When Tavin marks a job "pass: too senior" / "pass: defense, no thanks" / "pass: actually 90 min outside Denver":

1. Reason text appended verbatim to `preferences.md` with timestamp + job context (company, title, source URL).
2. Loaded into the scoring prompt for the next 30 runs as "recent rejections — avoid surfacing similar."
3. Promoted to "Learned patterns" section at top of `preferences.md` if the same shape shows up 3+ times. Weekly retro does this distillation pass.

Patterns become rules without Tavin having to formalize anything.

---

## Onboarding interview (first invocation)

When the skill is first invoked, Mizzix runs a one-time conversational interview to fill `criteria.md`. Bounded to ~10 questions, **one at a time**, per Mizzix's standard interview rhythm.

**Critical behavior:** When Mizzix already has a belief about Tavin from `tavin.md`, Job_Search/README.md, or interview-coach state, she frames it as **confirmation**, not a cold ask:

> *"You've said you'd take an AI-focused role even if it feels scary — still true? Anything to add?"*
>
> NOT *"Tell me about your AI interest."*

If the existing file is unambiguous and recent, skip the question entirely. Only ask cold for the gaps.

### Topics

Numbered for ordering, but Mizzix skips/merges based on what's already known:

1. **Defense contractors** — yes / no / depends on the work *(gap)*
2. **Public sector** — NASA / national labs / federal agencies in scope? *(gap)*
3. **Company stage** — early startup ok? IPO-stage? Big established? Mix? *(gap — README hints "founding-level appeals" but unconfirmed)*
4. **Travel willingness** — % travel cap? *(gap)*
5. **Specific exclusions** — companies, industries, cultures Tavin won't consider *(gap)*
6. **Visa / clearance** — confirm citizenship status, willingness for clearance-required roles *(likely fast confirm)*
7. **Compensation specifics** — confirm $70K floor, equity preference, target range *(extend from README)*
8. **Title aliases** — JD titles that should always trigger ("Mechanical Design Engineer," "Thermal Engineer," "ME I/II/III") *(gap)*
9. **Title exclusions** — titles to always skip ("Manager," "Sales Engineer") *(gap)*
10. **Geography refinement** — confirm Chicago/Milwaukee/Seattle/Denver, ask about cities not yet named that should be in scope (Boston? Austin? Phoenix? Houston?) *(gap)*

### Output

`criteria.md` with structured sections + a free-text "notes" block. Editable by hand at any time. The skill exposes an `update-criteria` command that re-runs only the relevant section if something changes.

---

## User-facing surfaces

### Morning brief — new section

Appears in the existing morning DM only if `job_matches.json` is non-empty.

```
**Job matches** (5 active, 2 new this morning)

🆕 1. **Mech Design Engineer** — Greenheck Group · Schofield, WI · $75-90K · score 4.2
       Hands-on HRSG-adjacent design, mid-IC. Schofield is small-city — flag.
🆕 2. **Thermal Engineer II** — Westinghouse · Cranberry, PA · $80-95K · score 4.0
       Nuclear thermal design, fits the HRSG translation story.
   3. **Mechanical Engineer** — Anduril · Costa Mesa, CA · $90-110K · score 3.8
       Defense + LA — flagging both. (carried from 5/10)
   4. ...
   5. ...

Reply "1 apply / 2 pass [reason] / 3 tomorrow" anytime, or wait for tonight's check-in.
```

Format rules:

- 🆕 marker on items new this morning; everything else is carried forward
- Score to one decimal; full 6-dim breakdown available on demand (`show me the breakdown for #2`)
- Concerns flagged inline ("LA — flagging," "small-city — flag") so Tavin doesn't need to remember dealbreakers
- "Carried from M/D" stamp on un-actioned older items so Tavin sees what's been sitting

### EOD check-in — ~7 PM DM

Fires from the heartbeat as a deterministic trigger when `job_matches.json` has any `surfaced` entries. Skips silently if zero.

```
End-of-day check-in — 5 jobs from this morning still open:

1. Greenheck — Schofield, WI
2. Westinghouse — Cranberry, PA
3. Anduril — Costa Mesa, CA
4. Lockheed — Denver, CO
5. Boeing — St. Louis, MO

For each, reply: applied / pass [reason] / tomorrow / decode
```

Tavin replies naturally — Mizzix parses with an LLM call (no rigid syntax). Examples that all work:

- *"1 applied, 2 tomorrow, 3 pass too defense-heavy, 4 pass location is actually 90min from Denver, 5 decode"*
- *"applied to 1, passing on 3 and 4, the rest tomorrow"*
- *"all pass except 2"* → Mizzix asks for reasons on the passes

### Actions

| Action | Result |
|---|---|
| **applied** | Move to `job_matches_history.json` with `applied_date`. Append row to `applications.md`. |
| **pass [reason]** | Move to history with `pass_reason`. Append to `preferences.md`. If no reason given, Mizzix asks once: *"Quick why on the Anduril pass?"* — short reason fine ("no thanks" works), but specifics improve the loop. |
| **tomorrow** | Stay in `job_matches.json`, increment `times_carried`. If it crosses 3, EOD asks: *"This one's been sitting 3 days — still considering or really a pass?"* |
| **decode** | Invoke interview-coach `decode <url>`. Match stays in queue with `decoded` flag; typically applied or passed after the decode. |

### Missed-reply nudge

If Tavin doesn't reply to the EOD by next morning, the next morning brief nudges once: *"Yesterday's matches still pending — want to check in?"* Beyond that, silent until he acts.

### CLI commands (called by Mizzix in conversation, and by the cron)

| Command | Trigger phrases | What it does |
|---|---|---|
| `scan` | "refresh job search," "look again now" | Runs the full pipeline ad-hoc (same as 3am cron) |
| `score-one <url>` | "what do you think of this posting [URL]" | Scores a single posting against current criteria; doesn't add to queue unless asked |
| `record-action <id> <action> [--reason TEXT]` | (called by Mizzix per parsed EOD reply item) | Updates state per actions table above |
| `list-active` | "what jobs do I have pending," "show me the queue" | Returns current `job_matches.json` formatted |

### Skill behaviors (Mizzix-driven, not CLI commands)

These flows are conversational — driven by `SKILL.md` instructions, executed by Mizzix herself, with file edits to vault assets:

| Behavior | Trigger phrases | What Mizzix does |
|---|---|---|
| Onboarding interview | (first invocation, or "let's redo my job criteria") | Runs the conversational interview per SKILL.md, writes `vault/projects/Job_Search/discovery/criteria.md` directly |
| Update criteria | "I'm done with Denver, drop it," "add Houston" | Re-runs the relevant interview section, edits `criteria.md` in place |
| Parse EOD reply | (Tavin replies to the 7pm check-in DM) | Parses reply conversationally, calls `record-action` once per item; if any reply is ambiguous or missing a pass-reason, asks one clarifying question per Mizzix's standard ambiguous-message rules |

### Handoff to interview-coach

When Tavin asks anything substantive about a specific match — *"should I apply to #3,"* *"tell me more about Westinghouse,"* *"prep me for Greenheck,"* *"decode #2"* — Mizzix invokes `interview-coach` with the JD URL/text. From there interview-coach owns the conversation per its existing multi-step intent rules (`decode → prep → resume`).

job-discovery's role ends at the handoff; the match stays in the queue until Tavin actions it through the EOD check-in.

---

## Implementation

### Dependencies (`pyproject.toml`)

```
python-jobspy        # multi-board scraper (LinkedIn, Indeed, Glassdoor, Google, ZipRecruiter)
claude-agent-sdk     # Mizzix-side scoring + EOD parsing (inherits OAuth)
pydantic             # state schema validation
pytest               # tests
```

**No Anthropic API key needed.** The Claude Agent SDK subprocess inherits Tavin's Claude Max OAuth, same pattern as `MizzixReflection` / `MizzixWeeklyRetro` already use. Keeps everything on the Max subscription, not pay-per-token.

**No Apify, no Bun, no Node.** Pure Python.

### Cron registration

New Windows scheduled task: **`MizzixJobDiscovery`**

- Trigger: daily at 3:00 AM (intentionally before `MizzixReflection` at 3:30 so reflection doesn't see incomplete state)
- Action: `python -m job_discovery.cli scan`
- Working dir: the repo
- Output: stdout/stderr appended to `vault/.mizzix_state/job_discovery.log`
- On failure: write `last_error` to `job_matches.json` metadata so morning brief can flag (*"Job scan failed at 3am — last successful run was M/D"*) instead of rendering stale data silently

### Failure-mode handling

| Failure | Behavior |
|---|---|
| One board errors (e.g., LinkedIn rate-limit) | Continue with others. Log per-board status. Brief shows: *"5 matches from 4/5 boards — LinkedIn skipped this run."* |
| All boards fail | No state mutation. Brief carries forward yesterday's queue with: *"No fresh scan today (all boards failed) — showing yesterday's pending."* |
| Scoring API call fails on a candidate | Fall back to deterministic rule-based score (keyword overlap with `criteria.md`). Mark `score_method: "fallback"` and surface only if rule score ≥ 4. Flag in brief one-liner. |
| Disk write fails | Log + retry once. If still failing, send Mizzix a heartbeat alert. |
| EOD reply ambiguous (parser unsure) | Ask one specific clarifying question per Mizzix's ambiguous-message rules. Don't act until confirmed. |
| Same JD posted on multiple boards | Dedupe key = normalized `(company + title + location)`. Picks highest-quality source (LinkedIn > Indeed > others) for the URL stored. |

### Initial setup checklist (for the build session)

1. Init git repo at `C:\Users\matis\Desktop\DevProjects\job-discovery\` with the structure above
2. `pip install -e .` and verify imports
3. Build minimal `cli scan --dry-run` (fetches but doesn't write state)
4. Run end-to-end against current `tavin.md` + `Job_Search/README.md` to verify scoring sanity (eyeball ~20 results)
5. Wire `vault/skills/job-discovery/SKILL.md` pointer
6. Run `onboard` interactively, fill `criteria.md`
7. First real `scan`, eyeball outputs, tune weights/thresholds if needed
8. Edit Mizzix `heartbeat.py` to add EOD trigger + morning-brief renderer (this is the "bot-affecting" change → first edit triggers session version bump per Mizzix conventions)
9. Register `MizzixJobDiscovery` scheduled task
10. Push repo to GitHub (private)
11. Manually fire EOD trigger to test parser end-to-end before letting it run autonomously

### Testing

- **Unit:** state read/write round-trips, dedupe logic, EOD parser against a fixture set of real-shaped replies
- **Integration:** scoring against ≥5 hand-labeled JDs (existing NE posting, a clearly-bad fit, a 50/50, etc.) — assert scores within ±0.5 of human label
- **No tests against live JobSpy fetches** (flaky, slow, third-party)
- **Manual smoke:** `--dry-run` flag on `scan` that fetches + scores but doesn't mutate state

---

## Decision log

Decisions made during the brainstorming session, kept here so future readers don't have to re-litigate.

| Decision | Why |
|---|---|
| Separate skill (`job-discovery`), not extending interview-coach | Interview-coach is already 38 commands and interactive-rhythm; discovery is automated/cron-driven. Different shapes of work. Mirrors gig-finder's external-skill pattern. |
| Daily 3am scan + top-N in morning brief (not on-demand-only) | The whole reason to wire it into Mizzix is the proactive push. On-demand-only would lose the value. |
| No silent dedup of un-actioned matches | Tavin wants matches he liked but hasn't acted on to keep showing — "tomorrow" status carries forward indefinitely until actioned. |
| EOD check-in (~7pm) for applied/pass/tomorrow + pass-reasons | Single-interaction-per-day model. Pass-reasons feed scoring loop. |
| JobSpy alone for discovery (no Job Scout, no Apify) | Job Scout's main value was its scoring; we're scoring Mizzix-side anyway. JobSpy covers the same boards (incl. LinkedIn) without an external API key. |
| Mizzix-side scoring, modeled on Job Scout's 6-dim rubric | Tavin: "Job Scout is specifically made for this task" — borrow its rubric structure. But scoring with Mizzix's living context (preferences, pass-history, interview-coach state) > generic Job Scout scoring. |
| Skip Job Tailor entirely | `interview-coach resume` already does JD-targeted optimization recommendations. Adding Job Tailor would duplicate scope, require YAML history migration, add Bun runtime dependency. |
| Skip Career-Ops | Pre-configured company list is AI/tech startups — useless for aerospace/mech. |
| Skip Role Scout | Requires `ANTHROPIC_API_KEY` → bypasses Max subscription billing. |
| Onboarding interview confirms (not asks cold) when Mizzix already has a belief | Per CLAUDE.md anti-hallucination + cite-sources rules. Avoids wasting Tavin's time on questions he's already answered in `tavin.md` / Job_Search README. |
| LA is downweighted (Location = 1), not hard-gated | Scotia full-time fallback in LA exists per Job_Search/README. Hard-gating LA would hide it permanently; soft-scoring lets exceptional LA roles surface anyway. |
| Spec lives in the new repo (`DevProjects/job-discovery/DESIGN.md`) | Ships with the code if the repo is ever shared. Tradeoff accepted: not searchable from vault. |

---

## Open questions

None at design time. The onboarding interview is the mechanism for resolving the remaining content questions (defense contractors, travel, title aliases, etc.) — they're inputs to the system, not design questions.

---

## Out of scope (call out so we don't accidentally build them)

- Resume tailoring (`interview-coach resume` covers it)
- Job Tailor / Job Scout / Career-Ops / Role Scout (rejected upstream)
- Auto-applying to roles (read-only — never touches application forms)
- Cover letter generation (interview-coach can produce drafts, doesn't need this skill)
- Email/recruiter outreach (`interview-coach outreach` covers it)
- Salary research / comp benchmarking (`interview-coach salary` covers it)
- Tracking *active* interview loops (Job_Search/README.md owns those — `applications.md` is just the dead-letter ledger of what was applied to)
