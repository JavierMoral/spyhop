# Checkpoints: pending work

Done:

- `spyhop checkpoints plan` (commits 3265e17, 0296ece, d65cb8a on the branch
  `checkpoints-plan`).
- The format of `CLAUDE-CHECKPOINTS.md`, `spyhop checkpoints check`, the new
  waiting reasons and the Git exclusion of the checkpoint files.

## Design decisions

- A checkpoint is a Claude Code subagent in
  `~/.claude/agents/checkpoint-<name>.md`. Its frontmatter gives `model`,
  `mode` (`read` or `exclusive`) and `after`.
- `after` only sets the order. A checkpoint does not get the context of another
  checkpoint.
- Each workflow lists its checkpoints in `checkpoints:` in `config.yaml`.
- All checkpoints use the worktree of the run. Spyhop creates no other
  worktree. An exclusive checkpoint runs alone in the worktree.
- Checkpoints only report. The orchestrator is the only agent that changes the
  code, and it makes a commit for each fix.
- The orchestrator writes one report per checkpoint in
  `CLAUDE-CHECKPOINT-<name>.md`, with one section per run and round, and the
  HEAD of each verdict.
- `/checkpoints` writes the last verdict of each checkpoint in its own file,
  `CLAUDE-CHECKPOINTS.md`. `CLAUDE-RESULT.md` stays free text and only refers
  to that file. The README gives the format.
- `spyhop checkpoints check` applies the control rule. The skill runs it at the
  end, and the daemon will use the same function.
- The Spyhop control does not accept a stale verdict, that is, a verdict whose
  HEAD is not the current HEAD of the worktree.

## Pending, in this order

1. [x] **Table format** and `spyhop checkpoints check`.
2. [x] **Catalog.** `checkpoint-regressions.md` (sonnet, exclusive),
   `checkpoint-mutation.md` (sonnet, exclusive, after regressions) and
   `checkpoint-security.md` (opus, read) in `~/.claude/agents/`. Each one gets
   `BASE`, `HEAD` and the worktree root from the orchestrator, and returns one
   JSON object: `verdict`, `head`, `commands`, `findings`.
3. [x] **Skill `/checkpoints`** in `~/.claude/skills/checkpoints/SKILL.md`.
   `spyhop checkpoints plan` now also prints `base_branch` (not committed
   yet). Decisions:
   - A checkpoint that comes after a failed one in the same round is skipped.
     The skill finds them with `spyhop checkpoints plan --from <failed>`.
   - After a fix, the next round runs all the checkpoints of the workflow,
     because a fix makes every old PASS stale.
   - The last round only checks. It does not fix.
   - The rounds stop early when all pass or when no finding needs a change.
4. [x] **Configuration and permissions.** (commits 347324d, e83fd96, 7146eba)
   - [x] `task.md`: the Review section runs `/checkpoints` when the workflow
     has checkpoints, after `review_command`. Deliverables and rule 6 name
     the checkpoint files.
   - [x] `goal.md`: the goal says "follow the Review section of
     CLAUDE-TASK.md", so the goal does not need the workflow.
     `agents.review_command` stays as it is; `/checkpoints` is not set there.
   - [x] `settings.local.json.j2`: allows `spyhop checkpoints plan` and
     `check`, `git rev-parse`, `git merge-base`, `git restore --source=`.
     Orca starts Claude with `--dangerously-skip-permissions`, so `allow`
     matters only if that changes. `mv` and `rm` are not in `allow`.
   - [x] Set `checkpoints:` in `~/.config/spyhop/config.yaml`: Bug and
     Impedibug [regressions, mutation, security], Technical Task
     [regressions, security].
   - [x] Installed from the branch `checkpoints-plan` with
     `uv tool install --reinstall .`, and restarted the service. After the
     merge to `main`, install again from `main`.
   - [ ] Make sure that `spyhop` is in the `PATH` of the Orca terminal
     (`~/.local/bin/spyhop`).
   - [ ] Test with a real run before step 5.
5. [~] **Completion control in Spyhop.** Mostly replaced by R2: the run is
   COMPLETED only after the checks phases after `implement` pass. Still open:
   at the end, also check the verdicts of the earlier phases (for example a
   `ticket` input that changed). With `updated` as the ticket fingerprint,
   that gives false stale verdicts, so decide first.
6. [x] **Replaced by R2.** Spyhop starts the checks itself, and
   `spyhop checkpoints rerun KEY` starts a failed round again.
7. [x] **`spyhop doctor`.** Make sure that each checkpoint of each pipeline
   has a valid file (`CheckpointPlanner.plan` over each checks phase).

## Workflow redesign

Proposal: an automated run is the execution of a workflow, and each step of
the workflow is a step of the pipeline. Checks can then run at any point, for
example before the plan. Do not start steps 5 and 6 before you decide this.

### Decisions to confirm

- Spyhop is the orchestrator, not a Claude session. The flow is fixed, so the
  control is in code. Spyhop keeps the state in the database, so a run
  survives the waits for a human.
- There are three types of step:
  - `agent`: plan, implement, review. It produces a file or commits, in an
    interactive Orca session.
  - `check`: it only reports one JSON verdict. It changes nothing, or it
    restores what it changes. It gets no context from other steps.
  - `human`: a gate, for example the plan review. The run goes to
    `WAITING_FOR_HUMAN`.
- A verdict is valid for the fingerprint of its inputs, not only for HEAD.
  Each check declares its inputs in its frontmatter (`inputs`):
  - `ticket`: the `updated` field of the JIRA issue.
  - `base`: the base SHA.
  - `plan`: the hash of `CLAUDE-PLAN.md`.
  - `head`: HEAD. The current rule is this case.
- Spyhop runs each check in headless mode and writes the verdict itself:
  `claude -p --agent checkpoint-<name> --output-format json --json-schema <schema>`.
  It can run in an Orca terminal, so that the user can see it. No Agent SDK
  and no new dependency.
- The fix loop is an attribute of a phase:
  `on_fail: {fix: implement, rounds: 3}`. A failed check resumes the
  implement agent with the reports, and then the checks run again.
- A failed check on the ticket (ticket-info, repro-steps) goes to
  `WAITING_FOR_HUMAN` (`on_fail: human`). Code cannot fix it, and the agent
  must not change JIRA.
- A workflow is an ordered list of phases in `pipelines:`. Workflows can share
  one pipeline. In a `checks` phase, `after` and `mode` work as now, so the
  planner stays. The frontmatter keeps only the properties of the agent:
  `model`, `tools`, `mode`, `after` and `inputs`.
- No general language with conditions, branches or expressions. Only linear
  phases, `after` in a phase, human gates and one fix loop.

Example:

```yaml
pipelines:
  bug:
    - checks: [ticket-info, repro-steps, reproduce]
      on_fail: human
    - agent: plan                 # prompt: bug_fix.md, phase A
    - human: plan_review
    - agent: implement            # phase B
    - agent: review               # /code-review-loop --rounds 2
    - checks: [regressions, mutation, security]
      on_fail: {fix: implement, rounds: 3}

projects:
  LPD:
    workflows:
      Bug:       {name: bug_fix,   pipeline: bug, trigger_statuses: ["In Progress"]}
      Impedibug: {name: impedibug, pipeline: bug, trigger_statuses: ["In Progress"]}
```

### Steps, in this order

R1. [x] **Input fingerprint of a verdict.** Add `inputs` to the frontmatter
    and store the fingerprint with each verdict. A verdict is stale when the
    fingerprint of its inputs changes. Step 5 needs this.
    - Inputs: `ticket`, `base`, `head`. The default is `[head]`, so the
      catalog does not change. `plan` (hash of `CLAUDE-PLAN.md`) is not done:
      no checkpoint uses it yet.
    - The `HEAD` column of `CLAUDE-CHECKPOINTS.md` is now `Inputs`, for
      example `ticket=<updated> base=<sha> head=<sha>`. An old table is
      not valid: run `/checkpoints` again.
    - `ticket` is the `updated` field of the issue. Any change of the issue
      makes the verdict stale (a comment, a label, a transition). This is
      safe but can give false stale verdicts. Change it to a hash of some
      fields if that is a problem in R4.
    - `spyhop checkpoints plan` prints `base`, `head` and the `inputs` of each
      checkpoint. The skill copies `inputs` to the table and uses `base` as
      `BASE`, so it does not find the merge base itself any more.
    - The CLI reads the JIRA credentials from the environment, or from
      `service.env_file`.
    - [x] Committed (3b4b617, 1f0c206, 28e4ed0) and installed again from the
      branch `checkpoints-plan`.
R2. [x] **Headless checks.** Decisions of the user:
    checks run in an Orca terminal, `checkpoints:` is replaced by `pipeline`,
    the same agent fixes the findings, `review_command` stays in implement.
    - `CheckRunner` (`application/checkpoint_runner.py`) runs a phase in
      rounds and stages. Each checkpoint runs in an Orca terminal with
      `spyhop checkpoints exec`, which runs
      `claude -p --agent checkpoint-<name> --output-format stream-json
      --json-schema <RESULT_SCHEMA> --dangerously-skip-permissions` and
      writes the result to `~/.local/state/spyhop/checkpoints/<run>/<row>.json`.
      A lock and the result file make a second start do nothing.
    - New table `checkpoint_runs` and columns `phase`, `fix_requested_at`,
      `checks_requested_at` in `automation_runs` (migration 4).
    - Spyhop validates the result, appends the report section and writes
      `CLAUDE-CHECKPOINTS.md` (columns Checkpoint, Verdict, Inputs, Round,
      Report).
    - A stage starts only on a clean worktree. After an exclusive checkpoint,
      HEAD and the status must not change. A timeout
      (`agents.checkpoint_timeout_minutes`, 180) is a FAIL.
    - `on_fail: fix`: `orca terminal send` gives the fix prompt
      (`templates/fix.md`) to the implementation agent. Its new
      `CLAUDE-RESULT.md` starts the next round. After `rounds`, the run
      waits with `checkpoints_failed`.
    - A failed round starts again when the inputs of a failed checkpoint
      change and no agent works, or with `spyhop checkpoints rerun KEY`.
    - Verified: `claude -p --agent` with `--json-schema` gives
      `structured_output` only when the agent's `tools` include
      `StructuredOutput`. The planner rejects `tools` without it, and the
      catalog has it. A real `spyhop checkpoints exec` with a temporary
      haiku checkpoint wrote a valid result.
R3. [x] **`pipelines:` in the configuration.**
    - Phases: `plan`, `plan_review`, `implement` and
      `{checks: [...], on_fail: human|fix, rounds: N}`. Rules in
      `validate_pipeline` (`domain/models.py`).
    - A workflow without `pipeline` keeps `plan_review` as before. With
      `pipeline`, `plan_review` is not allowed, and `checkpoints:` is an
      error.
    - The agent no longer runs `/checkpoints`. `CLAUDE-TASK.md` has a
      Checkpoints section: the checks before its work (read the reports)
      and after it. `spyhop checkpoints plan` prints the checks phases;
      `NAME...` and `--from` are removed.
    - [x] Migrated `~/.config/spyhop/config.yaml` (backup next to it): Bug
      and Impedibug use `pipeline: bug`, Technical Task
      `pipeline: technical_task`.
    - [x] Deleted the obsolete skill `~/.claude/skills/checkpoints/`.
    - [x] Committed (2dc6452, f0a8174, 472a0a0) and installed again from the
      branch `checkpoints-plan`.
R4. [ ] **Checks before the plan.** The plan agent reads their reports.
    - [x] Catalog in `~/.claude/agents/`:
      - `checkpoint-ticket-info`: sonnet, read, `inputs: [ticket]`. A
        checklist per issue type (Bug and Impedibug, or other types).
      - `checkpoint-repro-steps`: opus, read, after ticket-info,
        `inputs: [ticket, base]`. It maps each step to the code at `BASE`
        with `git grep` and `git show`. It does not run the steps.
      - `checkpoint-reproduce`: sonnet, exclusive, after repro-steps,
        `inputs: [ticket, base]`. It runs the steps against the bundle of
        the worktree, only on `localhost`, and only when `HEAD` is `BASE`.
        PASS means that the bug is reproduced.
      - They read the ticket with `<orca> jira issue <KEY> --json` and
        `<orca> jira comment list <KEY>`, with the command of
        `CLAUDE-TASK.md`. Orca does not give custom fields (affected
        version, components), so ticket-info checks only the description
        and the comments.
      - The three old checkpoints now have an explicit `inputs: [head]`.
    - [x] In the pipeline `bug` (Bug and Impedibug), with `on_fail: human`.
    - [ ] Test them on a real ticket.

### To verify

- [x] `claude -p` with `--agent` and `--json-schema` together (needs
  `StructuredOutput` in `tools`).
- [x] `claude --resume`: not needed. The fix prompt goes to the live agent
  with `orca terminal send`.
- [ ] The answer of `orca terminal create --json`: Spyhop reads `handle` at the
  top or under `terminal`, and does not need it. Not seen in a real call.
- [ ] `orca terminal send` to an idle Claude agent submits the prompt as a new
  turn. Not seen in a real call.
- [ ] A whole run with checks before and after the implementation.

## Optional

- [ ] From the code review: while a run waits on a failed checkpoint with the
  `ticket` input, each cycle sends one JIRA request and parses the agent
  files again. Cache the parsed checkpoints in `advance`, and read the
  ticket less often.
- [x] From the code review: the lookup of the workflow of a run
  (`issue_key.rsplit("-", 1)[0]` and `resolve_by_name`) is in four places.
  Add `WorkflowResolver.resolve_for_run(run)`.

- [ ] Keep a copy of the catalog in the Spyhop repository as examples. Now it
  lives only in `~/.claude/agents/`, which is not under version control.
- [ ] The three files repeat the input and output contract. If it changes,
  change all of them.

- [ ] `--failed`: run again only the failed or stale checkpoints.
- [ ] A progress comment on the Orca card for each checkpoint.
- [ ] A clear message when `plan` runs outside a worktree and the database does
  not exist. Now the message is "No database yet", without the `--workflow`
  hint.
