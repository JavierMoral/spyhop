<h1 align="center">🐋 spyhop</h1>

<p align="center">
  <strong>Turn JIRA tickets into Claude Code agents running in Orca.</strong><br/>
  A small local daemon that watches your tickets and starts the work when you do.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/managed%20with-uv-DE5FE9" alt="Managed with uv"/>
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20systemd-informational" alt="Linux and systemd"/>
</p>

---

*Spyhopping* is when an orca lifts its head out of the water to look around.
Spyhop does the same with JIRA. When you move one of your tickets to a start
status, such as **In Progress**, Spyhop creates a worktree for it in
[Orca](https://www.onorca.dev) and starts a Claude Code agent there.

You decide what gets worked on: you assign the tickets and you move them.
Spyhop never pushes, opens pull requests or changes tickets. The agent pushes
and opens a pull request only when you run `/pr` in its terminal.

## Table of contents

- [A ticket, step by step](#a-ticket-step-by-step)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
- [Troubleshooting](#troubleshooting)
- [Security](#security)

## A ticket, step by step

What you do is in **bold**. The rest happens by itself. Follow the run in its
[supervisor session](#the-supervisor-session): it tells you what happens, and
it runs the Spyhop commands that you ask for.

1. **Move a ticket assigned to you to a start status in JIRA.** Spyhop creates
   a worktree for the ticket and starts an agent on `agents.plan_model`. If the
   [pipeline](#pipelines-and-checkpoints) starts with checks, the agent starts
   after they pass. The agent gets the ticket and its parents in
   `CLAUDE-TASK.md`.
2. The agent writes `CLAUDE-PLAN.md` and declares it with
   `spyhop declare KEY plan`. Its Orca card moves to *In review*. The
   supervisor session gives you a summary of the plan. **Read the plan**,
   then do one of these:
   - **Approve it.** Tell the supervisor session that you approve the plan.
   - **Change it.** Give the changes to the supervisor session. It sends them
     to the plan agent.

   The implementation agent reads only `CLAUDE-PLAN.md`, so make sure that
   each change goes into that file. You can close the tab of the plan agent:
   the approval still starts the implementation, and the supervisor session
   starts a new plan agent for the changes (`spyhop replan`). The card only
   shows the state: moving it does not approve the plan.
3. Spyhop starts a new agent on `agents.model`. It implements the plan, makes
   local commits, writes `CLAUDE-RESULT.md` and declares the end with
   `spyhop declare KEY done`. If the workflow has
   [checkpoints](#pipelines-and-checkpoints) after the implementation, Spyhop
   runs them, and the agent fixes what they find. **Review the commits, then push and open the pull request**, yourself
   or with `/pr` in the agent's terminal.
4. **Close the ticket in JIRA.** Spyhop removes the worktree. See
   [When the ticket closes](#when-the-ticket-closes).

When a run is `WAITING_FOR_HUMAN`, the supervisor session tells you why and
gives the options. `spyhop status` also shows where each ticket is. The reason
tells you what to do:

| Reason | What to do |
| --- | --- |
| `plan_review` | Review the plan (step 2). |
| `checkpoints_failed` | Read `CLAUDE-CHECKPOINTS.md` and the reports. Fix the cause, then run `spyhop checkpoints rerun KEY`. |
| `agent_question` | Give your answer to the supervisor session. It shows the question of the agent when Orca gives it. |
| `unknown_state` | Tell the agent to go on in its terminal, or stop the run with `spyhop fail KEY`. |

Before a run waits with `unknown_state`, Spyhop tries once by itself
(`agents.max_restarts`). It starts an agent that Orca no longer lists again,
and it tells an agent that stopped without the plan or the end to continue.

To start a run again, stop it with `spyhop fail KEY`, then run
`spyhop retry KEY`. `retry` accepts only a finished run.

### The supervisor session

Each worktree gets one Claude session in an Orca tab named `spyhop`. Spyhop
sends it a message that starts with `[spyhop]` for each event: the new
worktree, the start of an agent or a checkpoint, each verdict, a run that
fails to start, the end of the run, and each reason why the run waits for you.
Spyhop knows what each agent does from the Claude Code hooks that it writes to
`.claude/settings.local.json` in the worktree: the hooks run `spyhop hook`,
which records the state of the agent. Orca tells whether the terminal of the
agent still exists.

A run that has no worktree yet has no session: for example, a run that waits
for disk space or for its worktree script, or that fails before its worktree
exists. Spyhop shows a desktop notification (`notify-send`) for it, and tells
the session about it once the worktree exists.

When the run waits, the session tells you what happened and gives the options
with their commands. When a plan waits for its review, the session gives a
summary of the plan and asks if you approve it. When you ask for changes to
the plan, the session sends them to the plan agent with `spyhop tell`.

The session keeps a table of the run: one row for each agent, each wait and
each checkpoint of each round. Each row gives the state, the result, a link to
the file that the step wrote, and the cause of a failure or a problem. The
session shows the full table again for each message, and after `retry`,
`fail` or `checkpoints rerun`. When the run completes, the session shows a
report: a description of the branch in 50 words or less, then the table.

The session starts without permission prompts and without the Edit, Write and
NotebookEdit tools. Its instructions tell it not to change files, and to run a
command only when you ask for it.

### When the ticket closes

When a ticket with a worktree closes, Spyhop stops the run and its agents and
removes the worktree. It never forces the removal:

- A merged branch is deleted with the worktree.
- An unmerged branch is kept. If you reopen the ticket, the new worktree
  continues from that branch.
- A worktree with uncommitted changes stays, as `CLEANUP_BLOCKED`, with a
  comment on its Orca card. Commit or discard the changes, and the next cycle
  removes it.

`CLAUDE-TASK.md`, `CLAUDE-PLAN.md`, `CLAUDE-RESULT.md`,
`CLAUDE-CHECKPOINTS.md`, `CLAUDE-CHECKPOINT-*.md` and
`.claude/settings.local.json` are excluded from Git, so they are lost with the
worktree.

## Requirements

- Linux with systemd.
- Python 3.12 or later, and [uv](https://docs.astral.sh/uv/).
- [Claude Code](https://docs.claude.com/en/docs/claude-code), as `claude` in
  the `PATH`. The agents, the supervisor session and the checkpoints run on it.
- [Orca](https://www.onorca.dev) with its CLI. Install the CLI from the
  settings of Orca. On Linux, its name is `orca-ide`.
- A JIRA Cloud account and an
  [API token](https://id.atlassian.com/manage-profile/security/api-tokens).

> [!WARNING]
> On many Linux desktops, `orca` is the **GNOME screen reader**. Spyhop uses
> only `orca-ide`.

## Installation

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh    # if you do not have uv
uv tool install git+https://github.com/JavierMoral/spyhop.git
spyhop version
```

To upgrade:

```sh
uv tool upgrade spyhop
systemctl --user restart spyhop
```

## Quick start

1. **Export your JIRA credentials**, for example in `~/.profile`:

   ```sh
   export JIRA_API_USER="you@example.com"
   export JIRA_API_TOKEN="your-api-token"
   ```

2. **Create the configuration.** This writes `~/.config/spyhop/config.yaml`,
   and copies the credentials into `~/.config/spyhop/env` for the service. It
   never overwrites a file.

   ```sh
   spyhop config init
   ```

3. **Edit `~/.config/spyhop/config.yaml`** with your JIRA site, repositories,
   projects and start statuses. See [Configuration](#configuration).

4. **Check everything**, and fix each ✗:

   ```sh
   spyhop doctor
   ```

5. **Try it in dry-run mode**, which is the default. The first poll only sets
   the starting point: Spyhop ignores tickets that you moved before it.

   ```sh
   spyhop run --once -v
   ```

6. **Install the service.** It starts when you log in.

   ```sh
   spyhop service install
   ```

7. **Go live.** Compare `spyhop status --all` with the tickets that you moved.
   When they agree, set `dry_run: false` and run
   `systemctl --user restart spyhop`.

Add Orca to the startup applications of your session. Until Orca answers,
the tickets that Spyhop finds wait.

## Configuration

Spyhop reads `~/.config/spyhop/config.yaml`. The complete example, with a
comment for each key, is
[`config.example.yaml`](src/spyhop/templates/config.example.yaml).

```yaml
application:
  polling_interval: 45            # seconds between polls
  max_concurrent_agents: 1
  dry_run: true                   # only log what Spyhop would do

jira:
  base_url: https://your-site.atlassian.net
  user_env: JIRA_API_USER         # names of the variables, never their values
  token_env: JIRA_API_TOKEN

repositories:                     # alias -> path on this machine
  main:
    path: ~/git/your-repository
    default_base_branch: master
    build_commands: ["./gradlew"] # commands that the agent can run

agents:
  plan_model: opus                # writes the plan
  model: sonnet                   # implements the plan

projects:
  PROJ:
    workflows:                    # keyed by JIRA issue type
      Bug:
        name: bug_fix
        trigger_statuses: ["In Progress"]
        repository: main
        prompt: bug_fix.md
        plan_review: true         # false: one agent plans and implements
```

You can override each value with an environment variable, for example
`SPYHOP_APPLICATION__DRY_RUN=false`.

Spyhop keeps its state in `~/.local/state/spyhop/`. The dry-run mode uses a
separate database, so Spyhop never acts on the tickets of a trial.

### Creating worktrees with a script

By default, Orca creates the worktrees. To use your own layout, set
`repositories.<alias>.worktree_script` to an executable. Spyhop runs it from
the repository with these variables: `SPYHOP_ISSUE_KEY`,
`SPYHOP_WORKTREE_NAME`, `SPYHOP_BRANCH`, `SPYHOP_BASE_BRANCH` and
`SPYHOP_REPOSITORY_PATH`. The script must:

- Check out `SPYHOP_BRANCH` when it exists. Otherwise, create it from
  `SPYHOP_BASE_BRANCH`.
- Write the absolute path of the worktree as the last line of standard
  output, and all other output to standard error.
- Print the same path again when the worktree already exists.
- Exit with 75 to try again on the next cycle, for example when a fetch
  fails. Any other non-zero code stops the run.
- Do the setup. Orca's setup hooks do not run, and `git worktree add` copies
  only tracked files. Copy the ignored files that a build needs, such as
  `build.<user>.properties`.

```bash
#!/usr/bin/env bash
set -euo pipefail

worktree="$HOME/git/your-repository/agents/$SPYHOP_WORKTREE_NAME/portal"

if [[ ! -e "$worktree/.git" ]]; then
	mkdir -p "$(dirname "$worktree")"
	git fetch --quiet upstream >&2 || exit 75

	if git rev-parse --verify --quiet "refs/heads/$SPYHOP_BRANCH" >/dev/null; then
		git worktree add "$worktree" "$SPYHOP_BRANCH" >&2
	else
		git worktree add -b "$SPYHOP_BRANCH" "$worktree" "upstream/$SPYHOP_BASE_BRANCH" >&2
	fi
fi

echo "$worktree"
```

To remove what the script creates next to the worktree, such as a bundle,
set `repositories.<alias>.worktree_cleanup_script`. Spyhop runs it after Orca
removes a worktree, with `SPYHOP_ISSUE_KEY`, `SPYHOP_WORKTREE_PATH`,
`SPYHOP_BRANCH` and `SPYHOP_REPOSITORY_PATH`. It runs for each worktree of
the repository, so it must ignore paths that are not its own. It must also
succeed when there is nothing to remove.

### Customizing templates

Spyhop writes the instructions for the agents from templates. To change one,
copy it from [`src/spyhop/templates/`](src/spyhop/templates/) to
`~/.config/spyhop/templates/` with the same name, and edit the copy. A
workflow's `prompt` can also name a new template. `spyhop doctor` lists the
templates that you changed.

### Pipelines and checkpoints

A pipeline sets the phases of a workflow, in order. A checks phase runs
checkpoints, which are Claude Code subagents that verify the work.

```yaml
pipelines:
  bug:
    - checks: [ticket-info, repro-steps, reproduce]
      on_fail: human              # the run waits for you
    - plan
    - plan_review
    - implement
    - checks: [regressions, mutation, security]
      on_fail: fix                # the agent fixes the findings
      rounds: 3

projects:
  PROJ:
    workflows:
      Bug:
        pipeline: bug             # instead of plan_review
```

After a fix, or a change of the ticket, the failed checks run again. To run
them again yourself, use `spyhop checkpoints rerun KEY`. Add `--failed` to run
only the failed and stale checkpoints: the round keeps each PASS whose inputs
did not change.

Each checkpoint is a file `~/.claude/agents/checkpoint-<name>.md`:

```markdown
---
name: checkpoint-mutation
description: Revert the production change and confirm that the new tests fail.
model: sonnet
tools: Bash, Read, Glob, Grep, StructuredOutput
mode: exclusive          # read: only reads the code, and runs in parallel
after: [regressions]     # checkpoints that run before this one
inputs: [head]           # ticket, base, head: a change makes the verdict stale
---
```

The checkpoint gets the base and head SHAs and the worktree root. It must
give a `verdict` (`PASS` or `FAIL`), the `head`, the `commands` that it ran
and its `findings`. A `PASS` without commands, or with a high or medium
finding, is a `FAIL`. Spyhop writes the verdicts to `CLAUDE-CHECKPOINTS.md`,
and each report to `CLAUDE-CHECKPOINT-<name>.md`. `spyhop checkpoints plan`
validates the checkpoints of a workflow, and `spyhop doctor` validates the
checkpoints of each pipeline.

### Earlier findings

The `knowledge` section connects Spyhop to a knowledge base of your own:

```yaml
knowledge:
  recall_command: /recall
  write_script: ~/bin/spyhop-write-finding
```

- With `recall_command`, `CLAUDE-TASK.md` tells the agent to run the command
  with keywords of the ticket before it plans.
- With `write_script`, a completed run stores each high or medium finding of a
  checkpoint that failed and then passed. The script gets the finding as JSON
  on stdin (`bucket`, `title`, `slug`, `tags`, `modules`, `jira`, `body` with
  `context`, `finding`, `evidence` and `why`, and `links`), and
  `spyhop-<run id>` as its argument. A failure only goes to the log.

## Usage

| Command | Description |
| --- | --- |
| `spyhop status [KEY]` | Show the active runs and their agents. `--all` also shows the finished runs. |
| `spyhop approve KEY` | Approve the plan of a run in `plan_review`. |
| `spyhop declare KEY plan\|done` | Declare that the plan or the work is ready. The agents run it at the end of their goal. |
| `spyhop replan KEY TEXT` | Start a new plan agent that changes `CLAUDE-PLAN.md`, for a plan in review whose agent is closed. |
| `spyhop tell KEY TEXT` | Send a message to the current agent of the run, in its Orca terminal. |
| `spyhop ticket KEY` | Show all the fields of a JIRA ticket, also the custom fields, and its comments. The agents and the checkpoints read the ticket with it. |
| `spyhop fail KEY` | Stop the active run. The agent and the worktree stay. |
| `spyhop retry KEY` | Start a finished run again, on the same worktree. |
| `spyhop cleanup KEY` | Remove the worktree of the ticket now. |
| `spyhop checkpoints rerun KEY [--failed]` | Run the current checks phase again. With `--failed`, run only the failed and stale checkpoints. |
| `spyhop checkpoints plan` / `check` | Show the checkpoints of the current worktree, or check their verdicts. |
| `spyhop run` | Run in the foreground. `--once` polls one time. `-v` shows debug logs. |
| `spyhop stats [--days N]` | Show the runs, agents and checkpoints of the last days: counts, times, PASS rate and the cost of the checkpoints. |
| `spyhop doctor` | Check the configuration and everything that Spyhop needs to run. |
| `spyhop config init` | Create the configuration and the credentials file. |
| `spyhop service install` / `uninstall` | Install and start, or remove, the systemd user service. |
| `spyhop version` | Print the version of Spyhop. |

To follow the logs of the service:

```sh
journalctl --user -u spyhop -f
```

## Troubleshooting

<details>
<summary><strong>Spyhop finds no tickets</strong></summary>

JIRA returns no error for a project, issue type or status that does not
exist. Run `spyhop doctor`: it names the values that do not agree with JIRA.
Status names are case-sensitive.
</details>

<details>
<summary><strong>Spyhop ignores a ticket that I moved before I installed it</strong></summary>

The first poll only sets the starting point. Move the ticket out of the start
status and back in.
</details>

<details>
<summary><strong>A closed ticket still has its worktree (<code>CLEANUP_BLOCKED</code>)</strong></summary>

The worktree has uncommitted or untracked files. Commit, stash or delete
them. The next cycle removes the worktree, or you can run
`spyhop cleanup KEY`.
</details>

<details>
<summary><strong><code>JIRA rejected the credentials (401)</code></strong></summary>

The token is wrong or expired. Update it in your session and in
`~/.config/spyhop/env`, then run `spyhop doctor`.
</details>

<details>
<summary><strong>The service does not start</strong></summary>

Run `spyhop service install` again, then read
`journalctl --user -u spyhop -b`. After 5 failures in 10 minutes, the service
stops.
</details>

## Security

- Spyhop only reads from JIRA. The token does not go into logs, the database
  or command lines.
- The ticket content is data, not instructions. A deny list in
  `.claude/settings.local.json` blocks some commands of the agent: `orca jira`
  commands that change the ticket, `git push --force`, `gh pr merge`,
  `gh pr close`, `git reset --hard` and `git worktree remove`. The list
  compares the start of each command, so it is a limit and not a guarantee.
  It does not cover MCP tools, such as a JIRA connector of Claude Code.
- Orca starts Claude Code without permission prompts, so only the deny list
  stops a command.
- A worktree is not a sandbox. The agents run with the permissions of your
  user.
