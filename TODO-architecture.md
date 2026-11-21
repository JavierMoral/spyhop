# Architecture: pending improvements

These items come from a review of the daemon, the supervisor, the repositories
and the Orca client. Do them one at a time. Mark each item when it is done, and
give the commit.

## Keep as it is

- One synchronous SQLite connection in the asyncio loop. It is not a
  bottleneck at this scale.
- The JIRA polling. A webhook needs a public endpoint, and the watermark covers
  the time when Spyhop is stopped.
- The checkpoints in systemd units, outside the service.

## Pending, in this order

1. [x] **Wake the daemon on local commands.** Today, `approve`, `declare`,
   `replan`, `checkpoints rerun` and the hooks wait for the next poll,
   which can be `polling_interval` seconds later. Add a wake event to the
   daemon loop with `loop.add_signal_handler(SIGWINCH, wake.set)`. Let the
   commands send `systemctl --user kill --kill-whom=main -s WINCH spyhop`.
   The default action of SIGWINCH is to ignore it, so an early wake does not
   stop the service. A failure of the signal is not an error. The JIRA poll
   and the cleanup stay on the poll interval, and a wake launches nothing
   while a JIRA rate limit lasts. Done in the branch `wake-daemon`.

2. [x] **Store the pipeline of the run, not only an index.** `run.phase` is an
   index into the pipeline of the current configuration, and
   `WorkflowResolver.resolve_for_run` reads that pipeline again on each cycle.
   A change of the pipeline while a run is active changes the meaning of the
   index. Store a copy of the resolved pipeline (JSON) when Spyhop creates the
   run, or store the phase name. Done in the branch `run-pipeline`: the launcher stores
   the pipeline (migration 14) when the first phase starts, and
   `resolve_for_run` uses it. For a run from before, the supervisor stores
   the pipeline of the configuration the first time that it sees the run.

3. [x] **Use a commands table, not flag columns.** `fix_requested_at`,
   `plan_changes`, `checks_requested`, `checks_failed_only`, `approved_at` and
   the notices queue are requests to the daemon in columns of
   `automation_runs`. Add a table
   `run_commands(run_id, kind, payload, created_at, consumed_at)`. The CLI,
   the supervisor session and the hooks write to it, and the daemon consumes
   it. The table also gives an audit trail for `status` and `stats`. Do this
   item together with item 6.

   Done in the branch `run-commands-and-events` (migration 16). The
   requests `approve`, `replan`, `checks` and `notice` are rows of
   `run_commands`, and the view `run_view` gives the run with the same fields
   as before. The supervisor consumes `approve` and `replan`, and a new round
   of checkpoints consumes `checks`. `fix_requested_at` stays a column: it is
   the state of the daemon, not a request.

4. [x] **Split `OrcaGateway` by responsibility.** Split its 15 methods into
   three protocols:
   - `WorktreeHost`: create, link and remove worktrees.
   - `TerminalHost`: launch an agent, list agents and panes, `send_prompt`,
     and the terminals.
   - `BoardMirror`: `mark_card`, `set_card_status` and `show_worktree`, only
     as a visual reflection.

   A tmux or headless host then implements only `TerminalHost`, and
   `BoardMirror` can be a no-op.

   Done in the branch `split-orca-gateway`. Each component gets only the
   roles that it uses, and `daemon.py` gives the same `OrcaClient` to each
   role. `show_worktree` is in `WorktreeHost`, because the supervisor uses
   it to find a removed worktree. `mark_card` is the only method of
   `BoardMirror`, because the CLI moves the card through `OrcaClient`.

5. [x] **Remove the per-run Orca calls in each cycle.** `_supervise` calls
   `show_worktree` for each run in each cycle. It needs the call only for the
   card fallback and to find a removed worktree. For runs with
   `signals = 'declare'`, use `worktree.path.exists()` and the result of
   `worktree_agents()`, which the cycle already has. The call goes away
   completely when the card fallback goes (see TODO-checkpoints.md).
   Done in the branch `fewer-orca-calls`: `worktree ps` also lists the
   worktrees without agents, so the supervisor calls `show_worktree` only for
   a card run, or when the worktree is not in the list.

6. [x] **Persist the in-memory state of the supervisor.** `_missing`,
   `_plan_agent_closed`, `_unalerted` and `_reconciled` are lost when the
   service restarts. A restart sets the count of missing cycles back to zero,
   and a wait message that did not reach the session depends on `_unalerted`.
   Store the data that decisions use in the run, for example `missing_since`
   and the pending alert.

   Done in the branch `persist-supervisor-state` (migration 15): the run
   stores `missing_cycles`, `missing_since`, `plan_agent_closed_told` and
   `alert_pending`. `_reconciled` stays in memory, because only the log uses
   it. An agent is now lost only after `MISSING_CYCLES` cycles and 90 seconds
   (`MISSING_AFTER`), because the wake can start several cycles in a few
   seconds. The 90 seconds count from the start of the service at the
   earliest, and a new agent clears the count.

7. [ ] **Move the logic out of `cli.py`.** `approve`, `declare`, `replan`,
   `retry` and `fail` decide and move cards in the CLI. Move that logic to an
   application service, `RunCommands`, so that tests do not go through typer
   and other callers can use it. Split `cli.py` (1155 lines) into
   `cli/runs.py`, `cli/checkpoints.py` and `cli/service.py`.

8. [ ] **Split `AgentSupervisor`.** It has 821 lines with observation, phase
   changes, restarts, replan, alerts and knowledge. Keep `observe()` as the
   pure core. Move the phase changes (`_enter`, `_launch`, `_checks`,
   `_request_fix`) to a `PhaseRunner` class, and the restarts (`_on_missing`,
   `_prompt_stopped`) to an `AgentRecovery` class. Do this item when other
   work changes these parts.

9. [ ] **Isolate the read-only checkpoints with systemd properties.** For the
   checkpoints with `mode: read` (security, regressions, repro-steps), add
   `ProtectSystem=strict`, `ReadWritePaths=<results_dir>`, `PrivateTmp=yes`
   and `NoNewPrivileges=yes` to the `systemd-run` command in
   `infrastructure/process/units.py`. Make sure that Claude Code still writes
   its own state in `~/.claude`.

10. [x] **Record run events.** Write the phase changes, waits, restarts and
    checkpoint results to a `run_events` table. Then `stats` and a new
    `status --timeline` read them with one query: time to the plan, rounds of
    checks, and restarts. This item uses the table of item 3.

    Done in the branch `run-commands-and-events`. `RunRepository` writes the
    events itself, so the callers did not change. `spyhop status --timeline`
    shows them, and `spyhop stats` gives the median time to the plan.
