# Implementation Facts (extraction pass, read-only)

Extracted 2026-08-06. Phase 8 work is IN PROGRESS and UNCOMMITTED (see §8/§9/§10).

## 1. COMMITS
```
8c0f901 docs: Phase 7 verification record + runbook for secret/fork items
55c233a security: scope PR_SENTINEL_LLM_API_KEY to model-calling steps only
9b48b61 style: ruff format the restructure changes (CI runs format --check)
48ffd47 docs: CLAUDE.md C2/C3 + Secrets section; retarget demo seed to pr-sentinel
727a0f0 caching + cost: intra-job prompt caching, real USD in the summary
16e28d6 workflows: restructure to analyze(extract)/publish(inference) split
f5bd702 triage: heuristic classifier + extract/refine split (option B)
689cc06 budget: dollar hard stop + Risk=unknown + triage-strategy flag
0056be9 checkpoint: pre-restructure, fan-out CLI green
ff2eea2 Phase 7: GitHub Actions workflows (CI, security, analyze/publish, command, nightly) + dependabot
2027b29 Phase 6: LangGraph pipeline, GitHub client, publishing
8f3e9b9 Phase 5: agents, prompts, grounding, dedup, scoring
f70ca20 / eaf3b60 Phase 3: LLM layer, budget governor, cache, replay
f575705 Phase 2: diff parsing, language detection, chunking
64b6ae1 Phase 1: settings, domain models, CLI skeleton
89b5b79 spec/build plan · a653e38 Anthropic default · 6304ad8 bootstrap
```
NOTE: no commit for Phase 8 yet — evals/, scripts/record_reference.py, 12 new golden
fixtures, and the parse_diff fix are all uncommitted.

## 2. MODULES
src/pr_sentinel/{__init__,models,settings,prompts,audit,cli}.py
src/pr_sentinel/agents/{__init__,base,triage,bug,security,style,improvement,critic,fixer,tools}.py
src/pr_sentinel/core/{__init__,chunking,dedup,grounding,language,scoring,triage}.py
src/pr_sentinel/gh/{__init__,client,context,diff,history,publish}.py
src/pr_sentinel/graph/{__init__,build,state}.py
src/pr_sentinel/guardrails/{__init__,injection,policy,redaction}.py
src/pr_sentinel/llm/{__init__,provider,anthropic,github_models,replay,budget,cache,json_mode}.py
src/pr_sentinel/evals/{__init__,harness,metrics,report}.py   (UNCOMMITTED)
- cli.py `report` command = STUB (`_pending("report","Phase 8")`, line ~1119).
- web/ package: DOES NOT EXIST (dashboard not built).
- agents/fixer.py present but not wired to any CLI command / slash command.

## 3. SETTINGS (PR_SENTINEL_* prefix; NAME = default)
LLM_PROVIDER = anthropic
MODEL = claude-sonnet-5
LLM_API_KEY = None
LLM_BASE_URL = None
GITHUB_TOKEN = None  (alias, unprefixed)
MAX_INPUT_TOKENS = 8000
MAX_OUTPUT_TOKENS = 4000
MAX_USD_PER_RUN = 1.00
MODEL_PRICES = {claude-sonnet-5, claude-opus-5, claude-haiku-4-5}  (STANDARD rates)
MAX_LLM_CALLS_PER_RUN = 12
RPM = 15 · RPD = 150 · MAX_CONCURRENCY = 5   (legacy, github_models only)
TRIAGE_STRATEGY = hybrid   (heuristic|llm|hybrid)
MAX_COMMENTS = 25
MAX_DIFF_LINES = 5000
MAX_FILE_BYTES = 1000000
CONFIDENCE_FLOOR = 0.55
CONTEXT_RADIUS = 8
CACHE_TTL_DAYS = 14
DISABLED = False

## 4. CLI (pr-sentinel <cmd>)
analyze     — run specialists over a PR diff, emit review payload
extract     — analyze-side: fetch PR, parse diff, HEURISTIC triage (no LLM/secret)
triage      — fetch PR, run LLM triage, write context artifact
refine      — publish-side: apply TRIAGE_STRATEGY to heuristic plan (LLM on unknowns)
agent       — run ONE specialist over context, write its result artifact
aggregate   — fan-in: merge agent results through grounding/critic/scoring
check-start — create in-progress 'PR Sentinel' check run early
check-fail  — mark check run failed (silent-failure path)
publish     — post review, summary comment, check run to a PR
local       — run one offline review case from a fixture dir
smoke       — one live LLM call to verify wiring + budget ledger
eval        — run golden suite, gate on CLAUDE.md thresholds (+cost/cache)
report      — STUB (_pending, Phase 8)

## 5. WORKFLOWS
ci-validation.yml — "CI Validation" — on push + pull_request — jobs: lint, types, test,
  build, results — every job permissions {contents: read}.
security-validate.yml — "Security Validation" — on workflow_run [CI Validation] — job:
  scan {contents: read}.
pr-review-analyze.yml — "PR Review Analyze" — on pull_request — job: extract
  {contents: read}. NO secrets, NO write, runs `pr-sentinel extract` only.
pr-review-publish.yml — "PR Review Publish" — on workflow_run [PR Review Analyze] — jobs
  in order: start {contents:read, checks:write} · agents {contents:read} · finalize
  {contents:read, pull-requests:write, checks:write}. LLM key step-scoped (refine/agent/
  aggregate steps only). Never checks out PR head (no `ref:`).
agent-command.yml — "Agent Command" — on issue_comment — job: review {contents:read,
  pull-requests:write, checks:write}; gate = job `if:` (OWNER/MEMBER/COLLABORATOR +
  startsWith '/sentinel review').
nightly-repo-report.yml — "Nightly Repo Report" — on schedule + workflow_dispatch — job:
  report {contents:read, issues:write, pull-requests:read}; calls stub `report` w/ fallback.

## 6. AGENTS (class · temp · risk levels run on · model)
Triage      · TriageAgent      · 0.0 · all files (assigns others) · settings.model
Bug         · BugAgent         · 0.1 · high, medium               · settings.model
Security    · SecurityAgent    · 0.0 · high, medium, low, unknown (NEVER skipped) · settings.model
Style       · StyleAgent       · 0.1 · high, medium               · settings.model
Improvement · ImprovementAgent · 0.2 · high (per CLAUDE.md)*      · settings.model
Critic      · CriticAgent      · 0.0 · merged finding set         · settings.model
Fixer       · FixerAgent       · 0.1 · opt-in, not wired          · settings.model
Model default = claude-sonnet-5 for ALL agents (single model; no per-agent override).
*core/triage.py risk→agents: high=[bug,security,style,improvement], medium=[bug,security,
 style], low=[security], unknown=[bug,security,style]. So Improvement runs on HIGH only. ✔

## 7. TESTS
Total collected: 339 tests (`pytest --collect-only`).
Coverage per package: MEASURED: NO — not run this pass (read-only constraint; would
require executing the suite).

## 8. REAL ARTIFACTS
Repo: https://github.com/ankita-hanamshet-dev/pr-sentinel  (public, single account)
- PRs reviewed by the system: NONE. Open PRs #1–#5 are Dependabot; none reviewed.
- PR Review Analyze runs: exist but ALL FAILED (pre-restructure, needed secret) e.g.
  https://github.com/ankita-hanamshet-dev/pr-sentinel/actions/runs/31100524276 (failure).
  PR Review Publish runs: ALL SKIPPED (gated on analyze success). No successful review run.
- Fan-out graph run URL (4 parallel agents converging): MEASURED: NO — never produced a
  successful analyze/publish run (secret unset).
- Sequential CI→Security PROVEN live: CI run 31113187208 finished 14:54:49Z; Security run
  31113236406 (workflow_run) started 14:54:54Z → after. Both success.
- LLM calls per PR (live): MEASURED: NO.  Tokens (live): MEASURED: NO.
  USD per run (live): MEASURED: NO.  Cache hit rate (live): MEASURED: NO.
  Review wall-clock (live): MEASURED: NO.
- Eval results (offline replay, from AUTHORED reference recordings — NOT a live model):
  recall 100%, precision 100%, F1 1.00, groundedness 100%, duplicate_rate 0%,
  max calls/PR 7, p95 latency 44ms, total cost $0.3577, mean $0.0275/case, cache hit 0%,
  gate PASS. Written to evals/results/20260806T171536Z.json.
  CAVEAT: numbers reflect the record_reference.py oracle, not real model recall/precision.
- Fork-PR path verified live: NO. Blocked on second account (Phase 7). Trust boundary
  proven statically only.
- Secret PR_SENTINEL_LLM_API_KEY: NOT SET (`gh secret list` empty).

## 9. NOT YET BUILT (Phase 8 / 9)
Phase 8 remaining:
- web/app.py FastAPI+HTMX dashboard — NOT STARTED (accessibility reqs unmet).
- scripts/seed_demo.py — NOT STARTED.
- docs/{ARCHITECTURE,DATA,RESPONSIBLE_AI,DEMO}.md — NOT STARTED.
- README.md — placeholder (~7 bytes), full version NOT written.
- `pr-sentinel report` command — STUB.
- Live golden `eval --record` run (real model) — NOT DONE (secret unset).
- Unit tests for evals/ (metrics/harness) — NOT WRITTEN.
Phase 9 (optional hardening) — NOT STARTED.

## 10. DIVERGENCES from CLAUDE.md (honest + specific)
- GOLDEN EVAL IS OFFLINE-AUTHORED: `eval --suite golden` passes against reference replay
  fixtures generated by scripts/record_reference.py (an oracle that emits each planted
  defect from expected_findings.yaml). It exercises grounding/dedup/scoring/cost/threshold
  code, but recall/precision are NOT a live model measurement. CLAUDE.md intends `--record`
  to capture LIVE responses; that requires the (unset) secret.
- `report` command + nightly report: stub, not implemented (CLAUDE.md lists both).
- No PR has ever been reviewed end-to-end live; the split analyze/publish path is proven
  only by unit tests + static audit, never a green live run.
- parse_diff CHANGED this session (uncommitted): now treats a bare empty line in a hunk
  body as a blank context line (git-apply semantics) so post-image numbering stays correct.
  Real GitHub diffs space-prefix blanks, so production unaffected; fix was needed because
  a hand-written fixture used empty context lines. Phase-2 byte-for-byte oracle re-run:
  MEASURED: NO this pass (was interrupted before verification).
- 3 previously-committed replay fixtures (911aff…, acbc90…, c8ffa9…) were OVERWRITTEN by
  re-recording — uncommitted modification to prior artifacts.
- Cost/cache metric is first-class in evals (amendment) but cache hit rate reads 0% under
  replay: replay fixtures don't carry cache_read/write tokens, so prompt caching cannot be
  exercised offline. A live run is required to observe a nonzero cache hit rate.
- Single-model design: every agent uses settings.model (claude-sonnet-5); `/sentinel deep`
  stronger-model re-run (CLAUDE.md) is not implemented.
- github_models provider retained as legacy (CLAUDE.md C1 documents this).
