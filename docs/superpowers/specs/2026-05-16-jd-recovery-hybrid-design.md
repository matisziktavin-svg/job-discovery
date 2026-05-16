# JD-Recovery Hybrid — Design Spec

*2026-05-16. Status: approved, pre-implementation.*

---

## Problem

The daily scan LLM-scores every fresh listing ([cli.py](../../../job_discovery/cli.py) loop, ~L152). When a board (LinkedIn in practice) scrapes a listing with **no description**, the scorer receives only title + company + location + salary. With no description text, unknown dimensions — seniority especially — default *optimistically* (mid-IC), so genuinely senior roles get a high score and slip into the brief looking like good mid-level matches.

Observed 2026-05-16: an Akkodis "Mechanical Design Engineer" listing (LinkedIn, empty description) scored **4.3 overall with seniority 5/5**. The real JD — recovered manually via WebFetch — requires **7–15+ years, Mid-Senior level**, missile-defense/hypersonic specialty, ITAR. The title carried zero seniority signal; the description that would have corrected it never made it into scoring.

A plain `requests`/JobSpy re-scrape does not recover these descriptions (LinkedIn walls server-side scraping — that absence is the root cause). A spike on 2026-05-16 confirmed Claude's **WebFetch** tool *does* retrieve the full JD for the exact failing Akkodis URL.

## Goal

Recover the missing description and re-score it correctly — but spend the (more expensive) WebFetch call only on listings where it actually matters, to stay within usage limits.

## Scope

In scope: a gated WebFetch + rescore step in the daily scan, plus a soft-penalty fallback when WebFetch also fails.

Explicitly out of scope (YAGNI):
- Salary correction from recovered JDs (the spike showed JobSpy salary can differ from the true posting — noted, not designed for).
- Parallelizing fetches (gate keeps the set tiny; revisit only if volume proves it).
- Any change to the scoring rubric, prompt, or rule-based fallback.
- Recovering descriptions for listings that scored ≤ 4 on blind defaults (accepted coverage gap — see Decisions).

## Decisions (resolved with Tavin, 2026-05-16)

1. **Gate criterion 1:** the JobSpy-scraped `description` is empty/whitespace.
2. **Gate criterion 2:** the blind (pre-fetch) `overall` score is **strictly > 4.0**. A senior role scoring 3.0–4.0 on blind defaults still surfaces unverified but sits lower on the list — accepted trade-off; the goal is protecting the top of the brief where Tavin acts.
3. **On WebFetch success:** set the recovered text as the listing description and re-run the existing scorer; the rescored result replaces the blind one.
4. **On WebFetch failure (empty / login wall / expired / timeout):** keep the listing on the brief, apply a **soft −0.5 downrank** and append a visible `⚠ unverified — JD unreadable` flag. Not dropped (don't silently lose listings behind a wall); not left at full optimistic score (don't reproduce the bug).
5. **Architecture:** WebFetch lives in its own isolated unit; the scorer stays deterministic and tool-free (Option A).

## Architecture

### New component: `job_discovery/fetch_jd.py`

One public function, no state writes, no input mutation:

```python
def fetch_job_description(url: str, timeout_s: float = 45.0) -> str | None
```

- Returns recovered JD text, or `None` for any non-success (unreadable page, sentinel, timeout, SDK exception). **Never raises.**
- Internals mirror the existing `score_llm` SDK setup ([score.py](../../../job_discovery/score.py) ~L356–416):
  - Pops `ANTHROPIC_API_KEY` from the environment first — **OAuth-consistency requirement** (same as score.py ~L369): the fetch must bill Tavin's Claude Max subscription, never a stray API key.
  - File-based system prompt (Windows 32KB-cmdline dodge, same pattern as score.py ~L380–386). Instruction: act as a JD extractor — WebFetch the URL, return only the description text (responsibilities, requirements, years, seniority, clearance). If login wall / empty / expired / no JD, return exactly `NO_DESCRIPTION_AVAILABLE`.
  - `ClaudeAgentOptions(allowed_tools=["WebFetch"], permission_mode="bypassPermissions", model="claude-haiku-4-5")`. **Haiku is deliberate**: extraction intelligence lives inside WebFetch's own model; the wrapping agent only calls the tool and relays text, so Sonnet would be wasted spend. The *rescore* still uses the normal scoring model.
  - Entire SDK receive loop wrapped in `asyncio.wait_for(timeout_s)`. Timeout / any exception / empty output / `NO_DESCRIPTION_AVAILABLE` present → return `None`.

### New scorer helper: `score.apply_unverified_penalty`

Pure function, mirrors `apply_salary_penalty` exactly ([score.py](../../../job_discovery/score.py) ~L234–273) — returns a new dict, no mutation:

```python
def apply_unverified_penalty(score_result: dict) -> dict
```

- `overall = max(1.0, round(overall - 0.5, 1))`
- Append `⚠ unverified — JD unreadable` to `one_line_take` (idempotent — don't double-append; cap at 200 chars like the salary helper).

**Verified invariant:** the gate is strictly `overall > 4.0`, so the minimum value that can reach this penalty is just above 4.0. After `−0.5` it lands ≥ ~3.5, always above the 3.0 surface threshold ([cli.py](../../../job_discovery/cli.py) `_select_top_n`, ~L84). A flagged-unverified listing therefore **always still surfaces** — it only stops outranking verified matches. This is exactly Decision 4, guaranteed by arithmetic.

### Integration point: `cmd_scan`

Inside the existing per-listing `try` block ([cli.py](../../../job_discovery/cli.py) ~L162–193), between the first `score_listing` and `apply_salary_penalty`:

```
result = score.score_listing(listing, criteria, preferences, profile_blob)

if not (listing.get("description") or "").strip() and result["overall"] > 4.0:
    jd = fetch_jd.fetch_job_description(listing["url"])
    if jd:
        listing["description"] = jd
        result = score.score_listing(listing, criteria, preferences, profile_blob)
    else:
        result = score.apply_unverified_penalty(result)

result = score.apply_salary_penalty(result, listing, criteria)
```

- Gate sits *before* `apply_salary_penalty` so a rescored result still gets salary treatment.
- A listing with a description, **or** scoring ≤ 4.0, never enters the branch — zero added cost for the common case.
- All of it is inside the existing per-listing isolation `try/except`, so even an unforeseen failure skips just that one listing and the scan/state-save continues.

## Data flow

```
fetch_all → hard_gates → dedupe vs seen → for each fresh listing:
    blind score_listing()
      └─ gate? (no desc AND >4.0)
           ├─ fetch_jd → text → set description → rescore → result
           └─ fetch_jd → None → apply_unverified_penalty → result
    apply_salary_penalty()
    assemble match dict
→ _select_top_n → _merge_with_carryforward → save_matches
```

No-double-fetch is already guaranteed by existing carry-forward: `filter_unseen` ([cli.py](../../../job_discovery/cli.py) ~L147) drops carried listings *before* scoring on later days, so a once-fetched listing is never re-fetched. No caching layer needed.

## Error handling / 3am robustness

- `fetch_job_description` self-contains its timeout and swallows all exceptions → worst case is the penalty path, never a crashed scan.
- 45s timeout, serial fetches; the `>4.0` gate bounds the qualifying set small enough that worst-case added wall-clock on the 3am batch is negligible.
- The existing per-listing `try/except` is the outer safety net.

## Testing

- **`test_fetch_jd.py`** (new), SDK mocked, no live network:
  - returns text on a normal tool response
  - returns `None` when the response contains `NO_DESCRIPTION_AVAILABLE`
  - returns `None` on timeout (mock `asyncio.wait_for` raising `TimeoutError`)
  - returns `None` on SDK exception
  - asserts `ANTHROPIC_API_KEY` is popped
- **`test_score.py`** additions for `apply_unverified_penalty`, mirroring the existing `apply_salary_penalty` tests: penalty applied; clamped at 1.0; flag appended; flag not double-appended; input not mutated.
- **`test_cli.py`** integration, mocking `fetch_jd.fetch_job_description` and `score.score_listing`:
  - gate fires *only* when description empty AND overall > 4.0
  - success path sets `listing["description"]` and re-scores
  - failure path applies `apply_unverified_penalty`
  - a normal listing (has description, or scored ≤ 4.0) never calls `fetch_jd`
- **Live smoke (not CI):** `fetch_job_description` against the real Akkodis URL (`https://www.linkedin.com/jobs/view/4412004839`); 2026-05-16 spike is the regression anchor — expect "7–15+ years / Mid-Senior".
