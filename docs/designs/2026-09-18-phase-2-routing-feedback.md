# Phase 2 design: routing feedback and multi-provider research

Status: reviewed proposal

Repository: `/Users/calebbolden/Projects/oss/agent-launchpad`

Companion integration: `/Users/calebbolden/Projects/agent-config`

## Decision

Phase 2 closes the loop between a routing mistake and a safer general router improvement.

Terminal Agent Launcher will own the feedback data, exact correction behavior, evaluator, candidate worktrees, promotion rules, and reports. Agent-config will be an optional proposal source. It can ask independent provider families for structured patches, but it will not receive write access or decide which patch wins.

Normal `tal` routing remains deterministic, local, and zero-token. A provider call happens only after an explicit `tal research` command.

## User experience

When the last task in the current terminal routed to the wrong project:

```bash
tal teach SecondBrain --last
```

`--last` displays the captured task, selected project, receipt ID, and age before asking for confirmation. It accepts only a receipt from the same TTY that is less than 15 minutes old. A script or non-interactive shell must use an explicit receipt or task instead:

```bash
tal teach SecondBrain --receipt ROUTE_RECEIPT_ID
```

When the task is not in recent route history:

```bash
tal teach SecondBrain --task "continue the knowledge capture work"
```

The command resolves `SecondBrain` against the current project catalog, records the correction locally, and installs an exact local override. Repeating the same normalized task routes correctly immediately. No model call is needed.

Corrections are visible and reversible:

```bash
tal teach --list
tal teach --show FEEDBACK_ID
tal teach --revoke FEEDBACK_ID
```

Revocation prints the task and expected project and requires confirmation in an interactive shell. `--yes` is available for scripts that already know the feedback ID.

Once one or more corrections have not been researched:

```bash
tal research --new-failures --provider workflow
```

The command asks the installed agent-config workflow runtime for independent patch proposals, evaluates each proposal locally, and leaves the winner on an `autoresearch/routing-*` branch with a report. It does not merge or reinstall the tool.

The built-in Codex provider remains available:

```bash
tal research --new-failures --provider codex
```

## System boundary

```text
task -> local router -> private route receipt
                         |
                         v
                  tal teach PROJECT
                         |
              durable feedback store
                 |               |
                 v               v
        exact local override   sanitized failure trace
                                 |
                                 v
                     read-only proposal providers
                                 |
                                 v
                     deterministic local tournament
                                 |
                                 v
                      candidate branch + report
```

Terminal Agent Launcher is authoritative for everything below the provider boundary. Providers return ideas as patches. They do not write files, see the private corpus, run the evaluator, commit, merge, or install anything.

## Architecture options

### Option A: owned feedback plane plus optional workflow adapter

This is the recommended and correct architecture.

- Terminal Agent Launcher owns a stable proposal protocol and all domain rules.
- Agent-config exposes a narrow, read-only routing-proposal adapter through an installed companion command.
- The launcher works without agent-config by using its built-in Codex provider.
- Claude and router-backed models can be added without importing TypeScript packages into the Python application.

This preserves the open-source tool's portability while reusing agent-config's model routing, provider independence, budgets, retries, sensitivity policy, and telemetry when installed. A generic patch-tournament abstraction is deferred until another real adopter needs it.

### Option B: teach support with the existing Codex provider

This is smaller. It adds corrections and exact overrides but leaves proposal generation single-provider. It delivers most of the user-facing value but does not use the multi-provider runtime.

### Option C: make agent-config a required dependency

This would put all proposal generation behind `workflow`. It removes duplicate provider code, but a personal workflow installation becomes mandatory for an otherwise standalone open-source CLI. That coupling is not justified.

## Local data ownership

Phase 1's ignored repository corpus remains supported as an import source. New corrections use a machine-level data directory so installed users do not need a source checkout.

| Data | Default path | Role |
|---|---|---|
| Durable feedback | `$XDG_DATA_HOME/terminal-agent-launcher/routing/feedback.jsonl` | Canonical teach and revoke events |
| Route receipts | `$XDG_STATE_HOME/terminal-agent-launcher/routing/receipts.jsonl` | Bounded per-terminal convenience state for `--last` |
| Research runs | `$XDG_STATE_HOME/terminal-agent-launcher/research/<run-id>/` | Reports, ledgers, proposals, and logs |
| Legacy private cases | `<source>/research/routing_cases.private.jsonl` | Optional Phase 1 import |

When XDG variables are unset, data defaults to `~/.local/share` and state defaults to `~/.local/state`.

Directories use mode `0700`. Feedback, receipt, lock, and manifest files use mode `0600`. Writes are atomic. Loaders reject symlinks, unsupported schema versions, malformed records, and project paths outside the current discovered catalog.

The feedback reducer and writer hold an operating-system advisory lock while reading or appending events. Research claims pending feedback under that lock, then releases it so `tal teach` can continue during a run. A separate repository-identity research lock allows only one research controller to advance branches for a checkout. Lock acquisition waits for two seconds and then exits with a clear busy error. Operating-system locks release on process death, so stale PID-file recovery is not used.

`tal teach` works from any installed package. `tal research` is different because it must create branches and disposable worktrees: it requires an explicit clean source checkout through `--repo /absolute/path`. An editable development install may use its detected checkout as the default. A wheel or package-manager install with no source checkout exits with the exact `--repo` command needed to continue.

## Feedback event contract

The feedback file is append-only JSONL. Corrections are never edited in place. The reducer processes records in file order and rejects duplicate event IDs, references to unknown feedback IDs, and invalid state transitions. A later event can revoke, claim, complete, or supersede an earlier teach event.

```json
{
  "schema_version": 1,
  "event": "teach",
  "event_id": "01J...",
  "created_at": "2026-09-18T21:00:00Z",
  "task": "continue the knowledge capture work",
  "task_fingerprint": "sha256:...",
  "normalizer_version": 1,
  "expected_project": {
    "id": "catalog-stable-id",
    "name": "KnowledgeVault",
    "path": "/Users/example/Projects/work/client/KnowledgeVault"
  },
  "observed": {
    "selected_project_id": "other-project-id",
    "score": 12.4,
    "margin": 1.2,
    "matched_terms": ["client"]
  },
  "profile_snapshot": {
    "projects": []
  }
}
```

The profile snapshot stores normalized name, path, and metadata terms plus document-term groupings. It does not store README bodies. The full catalog profile is required because the current router uses corpus-wide document frequency. Project keys inside the snapshot are pseudonymous. The expected project's live path remains local so the exact override can resolve it.

`normalize_task_v1` applies Unicode NFKC normalization, case folding, whitespace collapse, and trimming while preserving word order and punctuation. The raw task is retained in the private feedback record. Lookup supports every normalizer version still present in active feedback. A future normalizer must ship with an explicit append-only migration before an older implementation can be removed.

Research lifecycle records reference the original feedback ID:

- `research_claimed`: assigns a run ID, source commit, and lease expiry.
- `research_improved`: a verified winning candidate fixed a triggering case or reduced confident wrong routes without regression.
- `research_no_improvement`: a complete run found no measurable fix for that source commit.
- `research_degraded`: providers, evaluator, or verification could not complete; the feedback becomes pending again.
- `research_cancelled`: the user interrupted the run; the feedback becomes pending again.
- `research_interrupted`: recovery found that the owning process died; the feedback becomes pending again.
- `revoke`: disables the exact override and excludes the correction from future research.

`--new-failures` selects active teach events with no terminal research record for the current router commit. `research_improved` and `research_no_improvement` suppress another attempt at the same commit. A changed router commit makes `research_no_improvement` eligible again. Degraded, cancelled, interrupted, or expired claims are retryable. `--retry FEEDBACK_ID` explicitly retries an otherwise completed event.

Phase 1 private cases can be imported without copying or deleting the source file:

```bash
tal teach --import-private-corpus /absolute/path/routing_cases.private.jsonl
```

The importer validates every record before writing, computes a stable deduplication key from the normalized task, expected project, and profile snapshot, and appends evaluator-only events with a batch ID and source provenance. Repeating the import is idempotent. Imported cases do not become exact routing overrides unless the expected project resolves uniquely and the user adds `--activate`. `tal teach --revoke-import BATCH_ID` appends revocation events for the batch; it never rewrites the feedback file or removes the source corpus.

## Routing precedence

Direct user selectors keep their current behavior. For unresolved multi-word tasks, resolution becomes:

1. Exact normalized taught-task fingerprint.
2. Deterministic router.
3. Fail closed with ranked candidates.

A taught correction never performs fuzzy matching. If its expected project is no longer in the discovered catalog, routing stops with a stale-correction error. It does not silently fall back to the heuristic router.

`tal teach` records only explicit user corrections. Launch success, process exit status, or time spent in a project is not treated as implicit approval.

## Router seam

The scoring logic will be separated from filesystem profiling:

```python
build_profiles(projects, roots) -> tuple[RepositoryProfile, ...]
rank_task(task, profiles) -> RoutingEvidence
route_task(task, projects, roots) -> RoutingEvidence
```

`route_task` remains the production entrypoint. It builds profiles and delegates to `rank_task`. Feedback snapshots and research cases call `rank_task` directly, which reproduces the original catalog without rebuilding fake repositories or storing source documents.

The existing synthetic JSONL case format remains supported. The evaluator compiles old cases and captured feedback into the same profile representation.

## Provider protocol

Every provider receives one read-only request. Individual proposals still match `research/proposal.schema.json`, but Phase 2 wraps them in a versioned response envelope:

```json
{
  "protocol_version": 2,
  "request_id": "01J...",
  "request_sha256": "sha256:...",
  "target": "terminal_agent_launcher/routing.py",
  "proposals": []
}
```

The request contains:

- the current `routing.py` hash and repository-relative readable path;
- public training failures;
- a locally generated structural trace for private failures;
- recent experiment outcomes;
- the allowed repository-relative target path;
- proposal and wall-clock limits.

Private task text, project names, paths, customer identifiers, README contents, and private case IDs are omitted. A structural trace uses placeholders such as `TERM_1` and `PROJECT_2` while preserving term locations, document frequency, score contributions, expected selection, observed selection, and abstention state.

The controller rejects output whose request ID, request hash, protocol version, or allowed target does not match the active run. Private absolute paths never enter the request or response envelope.

Provider output is untrusted code. Before any proposal runs, an AST preflight rejects new imports outside the router's baseline allowlist, executable top-level statements beyond definitions and constants, and direct use of dynamic execution, subprocess, socket, or import primitives. Passing preflight is not treated as containment.

Evaluation runs in a disposable worktree under an operating-system sandbox backend. The child receives a temporary home directory, a minimal allowlisted environment, bounded CPU, memory, process count, file size, and wall time, no network, read access only to the disposable checkout, and write access only to its temporary result directory. It cannot read the feedback store, route receipts, agent credentials, SSH configuration, or the user's home directory. Phase 2 ships capability-probed backends for macOS (`sandbox-exec`) and Linux (`bubblewrap`). Multi-provider research fails closed when neither backend is available; bypassing containment is not a supported flag.

## Agent-config integration

Agent-config adds the narrowest adapter that satisfies this protocol. Phase 2 does not introduce a general arbitrary-patch workflow:

```text
tal-routing-proposals \
  --request /absolute/private/request.json \
  --workspace /absolute/read-only/worktree
```

The companion command lives in agent-config, uses the existing `WorkflowContext.call()` seam, and returns the versioned response envelope on stdout. Runtime events remain redacted JSONL on stderr. Agent-config's installer makes the command available beside `workflow`; Terminal Agent Launcher invokes it only for `--provider workflow`.

The graph requests two independent provider families per round. The second node runs after the first and declares independence from the first node's execution receipt. This serialization is intentional because the current runtime cannot guarantee family independence before the first receipt exists. Both nodes are read-only, have network denied, and may read only `terminal_agent_launcher/routing.py`, the public training corpus, and the sanitized request artifact.

Third-party router lanes follow agent-config's existing `auto` policy. They receive only public and structurally sanitized material. Raw private feedback is never an allowed sensitivity source.

If `tal-routing-proposals` is missing or its readiness check fails, `--provider workflow` fails visibly. It does not silently spend tokens through another provider. The user can rerun with `--provider codex`. If another project later needs the same tournament contract, the narrow runner can be extracted into a generic workflow as a separate decision.

## Tournament and promotion

Terminal Agent Launcher applies each proposal to a fresh disposable worktree at the current best commit. Candidates are never stacked until each is independently measured.

The deterministic ranking is:

1. Reject evaluator errors and any confident wrong route.
2. Reject any loss of correct routes or correct abstentions in private or holdout cases.
3. Prefer more correct private cases.
4. Prefer more correct public holdout cases.
5. Prefer more correct public training cases.
6. Prefer fewer nonblank lines in `routing.py`.
7. Break an exact tie with the patch SHA-256 so selection is stable.

Ranking chooses among candidates that already qualify as measurable improvements. A candidate is promotable only if it fixes at least one triggering private feedback case or reduces the number of confident wrong routes, while satisfying every non-regression rule above. Line count and patch hash are tie-breakers only; a smaller unchanged or worse router never advances. A round with no measurable improvement is dry.

Only the winning proposal in a round advances the research branch. It must pass `.codex-test-command` before commit. Losing proposals remain in the private run ledger with status and aggregate metrics, but private task text is not persisted in reports or provider logs.

The final branch remains human-gated. Phase 2 does not merge, push, open a pull request, or reinstall the CLI.

## Budgets and stop conditions

The multi-provider default is deliberately smaller than the single-provider Phase 1 loop:

| Limit | Default |
|---|---:|
| Provider families per round | 2 |
| Rounds | 5 |
| Total provider attempts | 10 |
| Concurrent providers | 1 |
| Node deadline | 8 minutes |
| Whole run | 45 minutes |
| Consecutive provider failures or timeouts | 2 |
| Dry rounds with no promotable candidate | 2 |

One controller owns the ledger across nested provider attempts. Retries consume the same ten-attempt budget. Once a stop condition fires, the run always writes a complete report. Exit status is `0` for a verified improvement or valid `no-improvement`, `2` for CLI or configuration errors, `3` for a degraded provider/evaluator/verification run, and `130` for interruption.

## Run state and recovery

Each research directory contains a mode-`0600` manifest written atomically. Its state is one of `starting`, `running`, `completed`, `no_improvement`, `degraded`, `cancelled`, or `interrupted`. It records the repository identity, source commit, owned temporary root, active worktree, claimed feedback IDs, provider receipts, candidate commits, and last completed stage.

On `SIGINT` or `SIGTERM`, the controller stops launching work, terminates active sandbox children, appends `research_cancelled`, preserves every verified candidate commit, removes its disposable worktree, and exits `130`. At startup under the repository research lock, the controller audits nonterminal manifests. If the recorded owner process is gone, it marks the run `interrupted`, appends `research_interrupted` for its claims, and removes a worktree only when its resolved path is beneath the recorded temporary root and its Git common directory and repository identity match the manifest. It never deletes a branch automatically.

Recovery is explicit:

```bash
tal research --list-runs
tal research --resume RUN_ID
tal research --abandon RUN_ID
```

Resume verifies the source commit, feedback snapshot, provider protocol, and existing candidate commits before continuing from the last completed stage. Abandon appends cancellation events, removes only verified owned temporary resources, and preserves branches and reports for inspection.

## Implementation milestones

### Milestone 2A: teach and exact correction

Add the private feedback store, route receipts, `tal teach PROJECT --last`, `tal teach PROJECT --task TASK`, revocation, stale-target handling, and exact override lookup.

Stop when a corrected task routes to the taught project with zero model calls, sensitive files have enforced modes, and malformed or stale records fail closed.

### Milestone 2B: reproducible captured cases

Extract `rank_task`, serialize bounded profile snapshots, compile Phase 1 cases to profiles, and let `tal research --new-failures` consume pending feedback without exposing raw task text to providers.

Stop when a captured failure reproduces the original ranking from its snapshot and private prompt-leak tests pass.

### Milestone 2C: multi-provider proposal tournament

Add the narrow `tal-routing-proposals` agent-config command, the Terminal Agent Launcher workflow adapter, sandboxed independent proposal evaluation, deterministic winner selection, shared budgets, and degraded-result reporting.

Stop when fake-provider integration tests prove family independence, attempt caps, timeouts, schema rejection, private-data exclusion, and stable winner selection. Then run one explicitly approved live two-provider smoke test.

## Required tests

- Exact taught tasks override the heuristic router; similar untaught text does not.
- Direct aliases and selectors retain precedence.
- A stale taught project is a terminal error.
- Feedback writes are atomic, private, versioned, and symlink-safe.
- Concurrent teach and research processes preserve all events and enforce one controller per repository.
- Every supported normalizer version reproduces its original fingerprint.
- Private-corpus import is validated, idempotent, reversible by batch, and inactive unless explicitly activated.
- Raw private fields never appear in provider requests, telemetry, reports, or logs.
- Captured profile snapshots reproduce the original route and scores.
- Response envelopes are bound to the active request ID, hash, protocol, and target.
- Provider patches can modify only `terminal_agent_launcher/routing.py`.
- AST preflight and the operating-system sandbox block filesystem, credential, process, and network escape attempts.
- A single confident wrong route disqualifies a candidate.
- Holdout and private regressions disqualify a candidate.
- Candidates without a measurable routing improvement never advance.
- Tournament ordering is stable regardless of proposal completion order.
- Attempt, timeout, and dry-round caps stop the run exactly.
- Degraded, interrupted, resumed, and abandoned runs produce the specified state, cleanup, claim, and exit-code behavior.
- No merge, push, install, or network call occurs outside the explicit provider run.

## Out of scope

- Background or scheduled research runs.
- Automatic merge, push, pull request, or installation.
- Fuzzy matching of taught corrections.
- Inferring feedback from agent exit status or user behavior.
- Uploading the private corpus or raw repository metadata.
- Letting a model modify the evaluator, corpus, promotion rules, or Git controller.

## Acceptance criteria

Phase 2 is complete when a user can correct one route with `tal teach`, receive the correct route immediately, run a bounded two-family research tournament, and get one verified candidate branch without private task text leaving the local feedback plane. All repository tests, the build, and a live opt-in workflow smoke must pass.
