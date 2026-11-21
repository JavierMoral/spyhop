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

7. [x] **Move the logic out of `cli.py`.** `approve`, `declare`, `replan`,
   `retry` and `fail` decide and move cards in the CLI. Move that logic to an
   application service, `RunCommands`, so that tests do not go through typer
   and other callers can use it. Split `cli.py` (1155 lines) into
   `cli/runs.py`, `cli/checkpoints.py` and `cli/service.py`.

   Done in the branch `split-cli-and-supervisor`. `RunCommands`
   (`application/run_commands.py`) has the logic of `approve`, `declare`,
   `replan`, `retry` and `fail`, and its own tests. `cli.py` is now the
   package `cli/`: `common.py` (the typer apps and the helpers), `runs.py`,
   `checkpoints.py` and `service.py`. Changes of behavior: each refusal of
   these commands now goes to stderr (`retry` wrote one to stdout),
   `declare` checks the step after it opens the database, and `approve`
   stores the approval of a card run only after the card moves.

8. [x] **Split `AgentSupervisor`.** It has 821 lines with observation, phase
   changes, restarts, replan, alerts and knowledge. Keep `observe()` as the
   pure core. Move the phase changes (`_enter`, `_launch`, `_checks`,
   `_request_fix`) to a `PhaseRunner` class, and the restarts (`_on_missing`,
   `_prompt_stopped`) to an `AgentRecovery` class. Do this item when other
   work changes these parts.

   Done in the branch `split-cli-and-supervisor`. `AgentSupervisor` keeps
   the observation and the signal table, and gets `PhaseRunner`
   (`phase_runner.py`), `AgentRecovery` (`agent_recovery.py`) and
   `RunAlerts` (`run_alerts.py`, the waits and the messages to the
   supervisor session). The code moved without a change of behavior, with
   one fix: when Orca cannot take a fix prompt, the next cycle sends it
   again.

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

## Second review, in this order

11. [ ] **One agent start for the launcher and the phase runner.**
    `AgentLauncher._launch` and `PhaseRunner.launch` repeat the same steps:
    `find_launching` or `create_launching`, `replay_expired`,
    `launch_agent`, `mark_launched` and `label_agent`. Only the handling of
    an error is different. Move the steps and the replay rule to one
    `AgentStarter` that returns the result, and let each caller handle the
    error.

12. [x] **Transactions in the repositories.** The connection commits each
    statement (`isolation_level=None`), and the CLI writes to the same
    database from another process. Some operations have more than one
    statement: `take_plan_changes` (read and consume), `_update` (status
    and event), and `CheckpointRunRepository.create` (row and consumed
    request). Add `with repository.transaction():` (`BEGIN IMMEDIATE`) and
    use it for these operations.

    Done: `transaction(connection)` in `database.py`. Each write of the
    repositories with more than one statement uses it, also the writes of a
    row and its event. A block in an open transaction is part of it.

13. [ ] **Remove the name of Orca from the domain.** The protocols are
    neutral, but `orca_worktree_id` (40 uses) and the exceptions
    `OrcaUnavailableError`, `OrcaUncertainError` and `OrcaCommandError` are
    in the models, the protocols and the application. Rename them to
    `host_worktree_id`, `HostUnavailableError`, `HostUncertainError` and
    `HostCommandError`, so that a tmux or headless host does not raise
    Orca errors. The database column needs a migration.

14. [ ] **Split `repositories.py` (993 lines).** Put each repository in its
    own module, and the row mappers and the pipeline JSON in
    `_mapping.py`. This is a move without a change of behavior.

15. [ ] **Move the worktree creation out of `AgentLauncher` (648 lines).**
    `_worktree`, `_scripted_worktree`, `_reopened`, `worktree_name` and the
    wait for disk space get a worktree for a run. Move them to a
    `WorktreeProvisioner`, so that the launcher only prepares the run and
    starts the agent.

16. [ ] **Move the checkpoint reports out of `CheckRunner` (695 lines).**
    `_validate`, `_contract_problem`, `_append_report` and
    `_write_verdicts` check the JSON of a checkpoint and write the files.
    Move them to `checkpoint_verdicts.py` or to a new
    `checkpoint_reports.py`, so that `CheckRunner` only runs the rounds.

17. [ ] **Inject a clock.** 32 calls read the time directly (`utc_now()`,
    `datetime.now`) in the repositories, the launcher, the Orca client and
    `agent_state`. The tests change times with SQL or set
    `_recovery._started_at`. Add a small `Clock` that the daemon gives to
    the components, and a fixed clock for the tests.

18. [ ] **Do not block the asyncio loop with git.** `infrastructure/git.py`
    calls `subprocess.run` from async code (`CheckRunner`, the janitor,
    `checkpoint_inputs`). A slow `git merge-base` on liferay-portal stops
    the whole cycle. Use the async `CommandRunner` or `asyncio.to_thread`.

19. [ ] **Integration tests against the real Orca.** `tests/integration/`
    is empty, and all the tests use `FakeOrca`. A change of the JSON of
    `orca-ide` is not found. Add a few tests with the mark `integration`
    that skip when Orca is not there: `worktree ps`, `worktree show` and
    `terminal send`.
