# Routing research

`tal research` runs bounded experiments against the deterministic task router. It borrows the useful loop from autoresearch: establish a baseline, try one change, measure it, then keep or discard it.

The production router remains local and uses no model tokens. Models participate only in an explicit research run.

## Run it

Start from a clean source checkout:

```bash
tal research --repo /absolute/path/to/terminal-agent-launcher
```

The defaults are 20 experiments or 60 minutes, whichever comes first. Each run creates an `autoresearch/routing-*` branch in an isolated Git worktree. The worktree is removed afterward. The branch, report, proposal files, test logs, and JSONL ledger remain available for review.

The controller does not merge the branch or reinstall `tal`.

## What the provider can do

Phase 1 supports Codex. The provider runs read-only in a temporary minimal workspace and returns a schema-validated unified diff. That workspace contains only the current `terminal_agent_launcher/routing.py`, the proposal schema, and public training rows. Holdout and private cases remain available only to the controller and evaluator. The controller accepts patches only for `terminal_agent_launcher/routing.py`.

This proposal interface is intentionally separate from experiment control. Its stable output contract is `proposal.schema.json`. A later adapter can use Claude or the multi-provider workflow runtime without changing evaluation, promotion, or Git handling.

## Corpora

The committed `routing_cases.public.jsonl` contains sanitized training and holdout cases. Put personal cases in `routing_cases.private.jsonl`; Git ignores that file and the controller never includes its task text in a provider prompt.

Start with the example:

```bash
cp research/routing_cases.private.example.jsonl research/routing_cases.private.jsonl
```

Each JSONL record contains:

```json
{
  "id": "private-my-correction",
  "task": "the task text that should route",
  "expected": "work/client/project-a",
  "projects": [
    {
      "path": "work/client/project-a",
      "metadata": {"README.md": "A small representative description."}
    },
    {
      "path": "work/client/project-b",
      "metadata": {"README.md": "Another project."}
    }
  ]
}
```

Paths are synthetic and relative to a temporary evaluation directory. Set `expected` to `null` when the safe behavior is to abstain. Private cases do not need a `split`; the controller treats all of them as protected holdout cases.

## Promotion rules

A candidate is discarded if it produces any confident wrong route or evaluator error. It also cannot reduce correct routes or correct abstentions in the public holdout or private corpus.

Among safe candidates, the controller prefers more correct routes, then more correct abstentions. If behavior is identical, it may keep a smaller implementation. Every kept candidate must pass the repository's `.codex-test-command` before the controller commits it.

Reports and logs default to `~/.local/state/terminal-agent-launcher/research/`. Private case contents are used by the local evaluator but are not written to the report or results ledger.
