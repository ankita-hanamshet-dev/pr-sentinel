# Phase 7 verification — workflow runs, not YAML review

Two records in one file: **Part A** is what was verified live/statically with evidence.
**Part B** is the runbook for the items that need a repo secret and/or a second GitHub
account — run them yourself once those are in place; each step says exactly what to
capture and what counts as a pass.

Repo under test: `ankita-hanamshet-dev/pr-sentinel`. Verified head: `55c233a`.

---

## Part A — verified with evidence

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | SHA pinning | **PASS** | All 6 actions are 40-char SHAs, zero tags; each confirmed to exist via `gh api repos/<action>/commits/<sha>`. checkout `3d3c42e…`, cache `55cc834…`, upload-artifact `043fb46…`, download-artifact `3e5f45b…`, github-script `3a2844b…`, setup-uv `c771a70…`. |
| 2 | Trust boundary | **PASS** | analyze: no `secrets.`, no `: write` (read-only). publish: no `ref:` on either checkout → default-branch code only, never PR head. analyze runs only `pr-sentinel extract` — no inference verb / `anthropic` / `models:`. |
| 3 | Secret hygiene | **PASS (after fix)** | Finding: key was workflow-/job-wide `env:`. Fixed in `55c233a` → `PR_SENTINEL_LLM_API_KEY` is step-scoped to refine / agent / aggregate(critic) / command-rerun only; check-start/publish/check-fail/uploads never receive it. No code path logs/serializes/comments the key (x-api-key header only); `secrets.*` auto-masked by GitHub. |
| 4 | Sequential orchestration | **PASS** | CI Validation [run 31113187208](https://github.com/ankita-hanamshet-dev/pr-sentinel/actions/runs/31113187208) finished `14:54:49Z`; Security Validation [run 31113236406](https://github.com/ankita-hanamshet-dev/pr-sentinel/actions/runs/31113236406) (`workflow_run`, same head `55c233a`) **started `14:54:54Z`** — after CI completed. |
| 9 | Slash-command gate | **PASS (code)** | `agent-command.yml` job `if:` = `issue.pull_request && contains(['OWNER','MEMBER','COLLABORATOR'], comment.author_association) && startsWith(body, '/sentinel review')`. Non-collaborator → `contains` false → job skipped silently (exit 0). Live non-collaborator case needs a second account (Part B, item 9b). |

---

## Part B — runbook for the secret/second-account items

### Prerequisite: set the one secret
```bash
gh secret set PR_SENTINEL_LLM_API_KEY --repo ankita-hanamshet-dev/pr-sentinel
# paste the Anthropic (or TCS) key at the prompt — never on the command line
gh secret list --repo ankita-hanamshet-dev/pr-sentinel   # confirm the NAME appears
```
Optional config (defaults shown): `gh variable set PR_SENTINEL_TRIAGE_STRATEGY --body hybrid`.

---

### Item 5 — fan-out / fan-in on a same-repo PR
```bash
git checkout -b verify/sqli main
mkdir -p app
cat > app/db.py <<'PY'
def get_user(conn, name):
    return conn.execute("SELECT * FROM users WHERE name = '" + name + "'")
PY
git add app/db.py && git commit -m "verify: planted SQL injection"
git push -u origin verify/sqli
gh pr create --fill --base main --head verify/sqli
```
Watch both sides:
```bash
gh run list --workflow=pr-review-analyze.yml --limit 1 --json databaseId,url,conclusion
gh run list --workflow=pr-review-publish.yml --limit 1 --json databaseId,url,conclusion
gh run view <publish_run_id>          # shows the job graph
```
**Pass:** the publish run graph shows `start → agent-bug | agent-security | agent-style |
agent-improvement` (4 in parallel) → `finalize`. The review posts an inline comment on
`app/db.py` flagging the injection.

**Forced single-agent failure (same PR branch):** publish runs *default-branch* tool code,
so break one agent on the branch by removing the prompt it loads, then re-push:
```bash
git rm prompts/security.prompt.yml && git commit -m "verify: break security agent"
git push
```
**Pass:** `agent-security` job goes red, but `finalize` still runs (`if: always() && start
succeeded`, `merge-multiple` downloads the 3 present artifacts) and the review still posts.
Restore with `git revert` when done. (The *graceful* in-artifact error path — where an agent
records an LLMError and `agent_errors` is populated while the job stays green — is covered by
`tests/test_cli_phase6.py::test_aggregate_fanin_merges_agents_and_errors`.)

---

### Item 6 — FORK PR (needs a second account)
From a **second GitHub account**: fork the repo, branch, plant a change, open a PR into
`ankita-hanamshet-dev/pr-sentinel`. Then, from the base repo, capture:
```bash
gh run list --workflow=pr-review-analyze.yml --limit 1 --json databaseId,url,event,conclusion
gh run view <analyze_run_id> --log | grep -iE "extract|context.json|pr-meta"
gh run list --workflow=pr-review-publish.yml --limit 1 --json databaseId,url,conclusion
gh api repos/ankita-hanamshet-dev/pr-sentinel/commits/<fork_head_sha>/check-runs \
  --jq '.check_runs[] | {name, status, conclusion, head_sha}'
gh api repos/ankita-hanamshet-dev/pr-sentinel/pulls/<n>/comments --jq '.[].line'
```
**Four pass criteria:**
1. analyze ran and its artifact contains `context.json` + `pr-meta.json`.
2. publish ran and a **`PR Sentinel` check-run is `in_progress` against the fork's head SHA**
   *before* inference finishes (poll the check-runs API a few seconds into the publish run).
   This is the highest-risk step; the head SHA is reachable in the base repo via
   `refs/pull/<n>/head`, which is why the early check works for forks.
3. The review posted with correct inline `line` numbers (verify against the diff).
4. **No secret on analyze** — confirm the analyze run had `PR_SENTINEL_LLM_API_KEY` empty:
   the `extract` step logs no model call, and the fork `pull_request` run receives no secrets
   by GitHub policy. (Static proof already in Part A, item 2.)

---

### Item 7 — concurrency: force-push cancels the stale publish
Concurrency key (publish): `sentinel-publish-${{ workflow_run.head_repository.full_name }}-${{
workflow_run.head_branch }}`, `cancel-in-progress: true`.
```bash
# with a publish run in flight for verify/sqli:
git commit --amend --no-edit && git push --force-with-lease
gh run list --workflow=pr-review-publish.yml --limit 2 \
  --json databaseId,status,conclusion,createdAt,url
```
**Pass:** the older publish run shows `conclusion: cancelled`; a newer one is queued/in-progress.
(Force-push → new analyze → new publish sharing the concurrency group → the stale one is
cancelled.)

---

### Item 8 — failure path: red check, not silence
publish runs default-branch code, so force `aggregate` to fail on **main** temporarily (e.g.
prepend `exit 1 &&` to the aggregate `run:` in `pr-review-publish.yml`, or gate it on a
`vars.PR_SENTINEL_FORCE_AGGREGATE_FAIL == 'true'` repo variable), then trigger a PR.
**Pass:** the PR still shows a **red `PR Sentinel` check** titled "review could not be
completed", with a `details_url` to the publish run — never a silent no-op. Revert the change
after. (The `check-fail` PATCH + audit record are unit-tested in
`tests/test_gh_publish.py::test_fail_check_run_marks_failure`.)

### Item 9b — non-collaborator command (needs the second account)
From the second (non-collaborator) account, comment `/sentinel review` on a PR. **Pass:** no
`Agent Command` run appears (`gh run list --workflow=agent-command.yml`) — the job `if:` gate
skipped it silently.

---

### Item 10 — cost + cache, run the same PR twice (the demo numbers)
With the secret set, push the PR once, let publish finish, then re-push (empty commit) to
re-run. For each publish run, read the finalize step summary:
```bash
gh run view <publish_run_id> --json url,jobs \
  --jq '.jobs[] | select(.name=="finalize") | .url'
# open the run's Summary tab, or:
gh api repos/ankita-hanamshet-dev/pr-sentinel/actions/runs/<publish_run_id>/jobs \
  --jq '.jobs[].steps[] | select(.name|test("Step summary")) | .name'
```
The summary prints **cost (USD at standard $3/$15 rates), findings, and cache hit/write**; each
agent artifact also carries `cost_usd`, `cache_hits`, `cache_writes`.
**Pass:** run 2 is dramatically cheaper than run 1 — the content-hash response cache skips
unchanged files (the dominant lever), and intra-job prompt caching lifts the cache-hit count on
any multi-chunk file. Record both runs' actual numbers for the demo.

> Expected order of magnitude (200-line PR, claude-sonnet-5 standard rates): run 1 ≈ **$0.20**;
> run 2 (one file changed) ≈ **$0.05**. Confirm against the real summary — do not quote the
> estimate as if measured.
