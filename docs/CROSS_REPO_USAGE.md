# Using PR Sentinel on another repository

PR Sentinel can review **any** GitHub repository's pull requests without that repo
vendoring, forking, or installing it. The two workflow templates in
`templates/consumer-workflows/` fetch the tool on demand with `uvx` and drive it entirely
over the GitHub REST API — there is **no checkout of pr-sentinel's source, no local
install, and no checkout of the reviewed repo's code**.

## Setup (3 steps)

1. **Copy the two templates** into the consuming repo, keeping the filenames:
   ```
   templates/consumer-workflows/pr-review-analyze.yml  ->  .github/workflows/pr-review-analyze.yml
   templates/consumer-workflows/pr-review-publish.yml  ->  .github/workflows/pr-review-publish.yml
   ```
   Their `name:` fields must stay `PR Review Analyze` / `PR Review Publish` — the publish
   workflow triggers on the analyze workflow by that name.

2. **Add one secret** on the consuming repo — *Settings → Secrets and variables → Actions
   → New repository secret*:
   ```
   Name:  PR_SENTINEL_LLM_API_KEY
   Value: <your Anthropic (or OpenAI-compatible) model key>
   ```
   That is the only secret. `GITHUB_TOKEN` is provided by Actions automatically and is used
   only for GitHub REST calls (reading the PR, posting the review, the Check Run).

3. **Open a PR.** Analyze runs on `pull_request`, publish runs after it via `workflow_run`
   and posts the review, the sticky summary comment, and the `PR Sentinel` Check Run.

Optional configuration, as **repository variables** (not secrets), all with sane defaults:
`PR_SENTINEL_MODEL` (default `claude-sonnet-5`), `PR_SENTINEL_LLM_PROVIDER` (default
`anthropic`), `PR_SENTINEL_TRIAGE_STRATEGY` (default `hybrid`).

## Fork PRs follow the same trust boundary as pr-sentinel itself

A PR opened from a **fork** of the consuming repo is handled exactly the way pr-sentinel
handles its own fork PRs:

- **Analyze** (`pull_request`) runs with `permissions: contents: read`, **no secret**, and
  **no write access**. GitHub withholds repository secrets from fork-triggered runs, so no
  model call and no posting can happen here — it only extracts the diff and runs heuristic
  triage. This is safe even though the PR contains untrusted code.
- **Publish** (`workflow_run`) runs in the **base-repo context**, so it has the secret and
  write scope. It never checks out the PR head; it reads `pr_number`/`head_sha` from the
  `pr-meta.json` artifact and the fork's head SHA is reachable in the base repo via
  `refs/pull/<n>/head`, which is what lets the early in-progress Check Run appear on the
  fork PR.

So fork PRs on your repo get reviewed, and an untrusted fork never gains access to your
`PR_SENTINEL_LLM_API_KEY` or write permissions.

| workflow | trigger | secret? | write scope | checks out PR head? |
|---|---|---|---|---|
| analyze | `pull_request` | no | no | no |
| publish | `workflow_run` | yes (step-scoped) | `pull-requests`, `checks` | no |

## ⚠️ Pin `@main` before using this beyond a demo

Every tool invocation in the templates uses:
```
uvx --from "git+https://github.com/ankita-hanamshet-dev/pr-sentinel.git@main" pr-sentinel ...
```
`@main` means **your repo's review behavior tracks pr-sentinel's `main` branch and can
change at any time with no change on your side** — new heuristics, new prompts, different
cost, even a regression, all arrive silently on your next PR.

For anything past a demo, **pin `@main` to an immutable reference** in every `uvx --from`
line — there are **7** of them (**1** in `pr-review-analyze.yml`, **6** in
`pr-review-publish.yml`):

- **A commit SHA (strongest, immutable):** `...pr-sentinel.git@<40-char-sha>`
- **A release tag:** `...pr-sentinel.git@v1.2.3` — convenient, but a tag can be moved, so
  it is only as trustworthy as the tagging process.

Bump the pin deliberately (e.g. via Dependabot on the git ref, or a scheduled review) so
upgrades are a reviewed change, not a surprise.

## Verify the review actually ran

On the PR's **Checks** / **Actions** tab you should see, in order:

1. **PR Review Analyze** (`pull_request`) — completes in seconds. Its log shows heuristic
   triage only: a line like `wrote context.json: N files triaged (0 unknown), N hunks`, and
   **no** reference to `PR_SENTINEL_LLM_API_KEY` and no call to `api.anthropic.com`. That is
   expected — inference does not run on this side.
2. **PR Review Publish** (`workflow_run`) — posts an in-progress **`PR Sentinel`** Check Run
   within seconds, then (typically 2–3 min) the full review: inline comments, a sticky
   summary comment (marked `<!-- pr-sentinel:summary -->`), and the final Check Run.

The final Check Run conclusion tells you what happened at a glance:

| Conclusion | Meaning |
|---|---|
| **success** (green) | reviewed, no findings and no escalation |
| **neutral** | reviewed with findings, or human review requested, but nothing critical |
| **failure** | a **critical** finding exists (e.g. SQL injection, hardcoded secret) |

The publish job's **step summary** prints the run's real USD cost (at standard $3/$15 per-1M
rates), the cache hit/write counts, and `agent_errors`.

## Troubleshooting

- **Publish never runs after Analyze.** The `workflow_run` trigger binds by workflow *name*.
  Confirm the `name:` fields are exactly `PR Review Analyze` and `PR Review Publish` and that
  the filenames were kept. A mismatch fails silently with no error.
- **`neutral` Check Run with "Human review: required" and 0 findings on obviously buggy
  code.** A specialist could not reach the model — almost always a missing, invalid, or
  rate-limited `PR_SENTINEL_LLM_API_KEY`. Open the publish run and check `agent_errors` in the
  step summary / per-agent artifacts; a `LLM HTTP 401` there means the key is bad or over its
  limit. (By design this surfaces as human-review-required, **never** as a false green
  `100/100` — a failed run is always visible.)
- **Secrets are per-repository.** Setting the key on one repo does **not** cover another.
  Each consuming repo needs its own `PR_SENTINEL_LLM_API_KEY`. Re-run after adding it by
  pushing any commit to the PR branch (or re-running the Analyze workflow).
- **Fork PR shows Analyze but no findings from Analyze.** Correct and expected — Analyze is
  heuristic-only and secretless; the review is posted by Publish from the base-repo context.

## Notes

- **Cost.** Each run is bounded by `PR_SENTINEL_MAX_USD_PER_RUN` (default `$1.00`) and the
  secondary `MAX_LLM_CALLS_PER_RUN` (default `12`). The publish step summary prints the
  actual USD (standard $3/$15 per-1M rates) and cache hit/write counts.
- **Caching.** The publish jobs persist the LLM response cache (`.sentinel/cache.sqlite`)
  via `actions/cache`, so re-pushes that touch few files stay cheap. The cache key is not
  prompt-hashed here because prompts ship inside the uvx-fetched package, not your repo.
- **Cold start.** `uvx` builds pr-sentinel on the first step of each job, adding a small
  amount of setup time per run; pinning to a SHA also makes that build reproducible.
- **Check Runs** require the Actions `GITHUB_TOKEN` (a GitHub App token, which Actions
  supplies automatically); a personal PAT would get a 403. No action needed.
