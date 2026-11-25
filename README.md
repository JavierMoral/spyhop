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

What you do is in **bold**. Spyhop does the rest. Follow the run in its
[supervisor session](#the-supervisor-session).

1. **Move a ticket assigned to you to a start status in JIRA.** Spyhop creates
   a worktree for the ticket and starts a plan agent.
2. The agent writes `CLAUDE-PLAN.md`, and the supervisor session gives you a
   summary of it. **Read the plan**, then tell the supervisor session to
   approve it, or give it the changes. Make sure that each change goes into
   `CLAUDE-PLAN.md`, because the next agent reads only that file.
3. Spyhop starts an agent that implements the plan, makes local commits and
   writes `CLAUDE-RESULT.md`. If the workflow has
   [checkpoints](#pipelines-and-checkpoints), Spyhop runs them, and the agent
   fixes what they find. **Review the commits, then push and open the pull
   request**, yourself or with `/pr` in the agent's terminal.
4. **Close the ticket in JIRA.** Spyhop removes the worktree. See
   [When the ticket closes](#when-the-ticket-closes).

When a run needs you, its status is `WAITING_FOR_HUMAN`. The supervisor
session and `spyhop status` tell you the reason:

| Reason | What to do |
| --- | --- |
| `plan_review` | Review the plan (step 2). |
| `checkpoints_failed` | Read `CLAUDE-CHECKPOINTS.md` and the reports. Fix the cause, then run `spyhop checkpoints rerun KEY`. |
| `agent_question` | Give your answer to the supervisor session. |
| `unknown_state` | Tell the agent to go on in its terminal, or stop the run with `spyhop fail KEY`. |

To start a run again, stop it with `spyhop fail KEY`, then run
`spyhop retry KEY`.

### The supervisor session

Each worktree has a Claude session in an Orca tab named `spyhop`. It tells you
each event of the run and why the run waits, with the options and their
commands. It keeps a table with the state and the result of each agent and
each checkpoint. When the run completes, it shows a short description of the
branch.

Ask the session to approve the plan, to send changes to an agent, or to run
other Spyhop commands. The session does not change files, and runs a command
only when you ask for it.

Before the worktree exists, a run has no session. Spyhop shows a desktop
notification for it instead.

### When the ticket closes

When a ticket closes, Spyhop stops its agents and removes the worktree:

- A merged branch is deleted with the worktree.
- An unmerged branch is kept. If you reopen the ticket, the new worktree
  continues from that branch.
- A worktree with uncommitted changes stays as `CLEANUP_BLOCKED`. Commit or
  discard the changes, and Spyhop removes it.

The `CLAUDE-*.md` files are not in Git, so they are lost with the worktree.

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
    - checks: [format, regressions, verify-fix, mutation, variants, security]
      on_fail: fix                # the agent fixes the findings
      rounds: 3

projects:
  PROJ:
    workflows:
      Bug:
        pipeline: bug             # instead of plan_review
```

A run keeps the pipeline that it started with. A change of `pipelines:`
applies to the runs that start after the change. The workflow of the run and
the checkpoint files stay in use: a run whose workflow or checkpoint is
removed waits for you.

After a fix, or a change of the ticket, the failed checks run again. To run
them again yourself, use `spyhop checkpoints rerun KEY`. Add `--failed` to run
only the failed and stale checkpoints: the round keeps each PASS whose inputs
did not change.

Spyhop owns its checkpoints, skills, and the scripts of its subagents, in
[`src/spyhop/claude/agents/`](src/spyhop/claude/agents/),
[`src/spyhop/claude/skills/`](src/spyhop/claude/skills/), and
[`src/spyhop/claude/scripts/`](src/spyhop/claude/scripts/). Claude Code reads
them from `~/.claude/agents/`, `~/.claude/skills/`, and `~/.claude/scripts/`,
so link them there:

```sh
spyhop claude install
```

The command replaces a link that already exists, and keeps a file or
directory that is not a link. Run it again after each installation of Spyhop.
With `uv tool install --editable .`, the links point to the repository, and a
change of a checkpoint applies without a new installation.

Each checkpoint is a file `checkpoint-<name>.md`:

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
| `spyhop status [KEY]` | Show the active runs and their agents. `--all` also shows the finished runs. `--timeline` shows the history of each run. |
| `spyhop approve KEY` | Approve the plan of a run in `plan_review`. |
| `spyhop replan KEY TEXT` | Change a plan in review when its agent is closed. |
| `spyhop tell KEY TEXT` | Send a message to the current agent of the run. After a completed run, start a new run where a new implementation agent makes the change. |
| `spyhop ticket KEY` | Show a JIRA ticket with all its fields and comments. |
| `spyhop fail KEY` | Stop the active run. The agent and the worktree stay. |
| `spyhop retry KEY` | Start a finished run again, on the same worktree. |
| `spyhop cleanup KEY` | Remove the worktree of the ticket now. |
| `spyhop checkpoints rerun KEY [--failed]` | Run the checkpoints again. With `--failed`, run only the failed and stale checkpoints. |
| `spyhop run` | Run in the foreground. `--once` polls one time. `-v` shows debug logs. |
| `spyhop stats [--days N]` | Show the counts, times and costs of the runs of the last days. |
| `spyhop doctor` | Check the configuration and everything that Spyhop needs to run. |
| `spyhop config init` | Create the configuration and the credentials file. |
| `spyhop claude install` | Link the subagents, skills, and scripts of Spyhop in `~/.claude`. |
| `spyhop service install` / `uninstall` | Install and start, or remove, the systemd user service. |
| `spyhop version` | Print the version of Spyhop. |

The agents use `spyhop declare` and `spyhop checkpoints plan` / `check`. You
do not need them.

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
