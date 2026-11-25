---

allowed-tools: [Agent, Bash, Edit, Glob, Grep, Read, Skill, Write]
argument-hint: "[optional target] [--rounds N] [--effort LEVEL] [--min-severity LEVEL] [--allow-dirty] [--fresh]"
description: Run /code-review, the custom-pr-reviewer and custom-pr-reviewer-naming agents, and the pr-reviewer rules over the branch in a loop, applying what is genuinely actionable each round, until a round comes back with nothing left worth doing. Use when the user invokes /code-review-loop, or asks to review and fix until the reviewers are quiet, to iterate the reviewers to convergence, or to clean a branch up before /pr-check.
name: code-review-loop

---

# Code Review Loop

Run the three reviewers over the branch, apply what is genuinely actionable, and run them again — until a round comes back with nothing left worth doing.

The reviewers are stateless. Every round re-reads the whole diff and re-raises whatever the last round deliberately declined, so a loop that only counts findings never terminates: the same three opinions come back forever. **Convergence comes from the ledger, not from the reviewers.** A verdict, once reasoned, is recorded against the finding and never re-litigated — and a round whose every finding is already settled is a converged round, however many findings it returned.

The run is autonomous end to end. Triage is the skill's to make, under the bar written below, and the user sees every decision once at the end, in the **Final Report**, rather than a question per round.

## When To Use This Skill

Activate on `/code-review-loop`, or when the user asks to review and fix until the reviewers stop complaining, to iterate the reviewers to convergence, or to clean a branch before `/pr-check`.

## Input

`${ARGUMENTS}` holds an optional target and the flags.

The target is a path, a directory, a ticket key, or the literal `staged`, and is passed to `/code-review`, `custom-pr-reviewer`, and `custom-pr-reviewer-naming` verbatim. With none given, they default to the current branch against the integration ref, which is the intended scope.

The pr-reviewer takes only a commit range or `--staged`. For the target `staged`, give it `--staged`. For all other targets, give it `"${merge_base}..HEAD"`. When the target is a path or a directory, keep only the violations whose `file` is in that path.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--rounds N` | 3 | Hard cap on rounds. Each round costs three reviewers plus a `formatSource`, so raise it deliberately |
| `--effort LEVEL` | `high` | Passed to `/code-review`. `high` and `max` broaden coverage and return uncertain findings, which the bar below is written to absorb |
| `--min-severity LEVEL` | `low` | Apply only the findings with this severity or a higher one: `critical`, `high`, `medium`, or `low`. See **Severity** |
| `--allow-dirty` | off | Accept that uncommitted tracked changes mix with the edits of this run |
| `--fresh` | off | Drop the existing ledger and re-litigate every verdict from scratch |

Resolve the base once and reuse it:

```bash
integration_ref=origin/master
merge_base=$(git merge-base "${integration_ref}" HEAD)
```

Use the remote-tracking ref, never a local branch of the same name — a local `master` that lags pulls commits the branch never added into the diff, and one that leads hides commits it did.

## Preconditions

Check both before the first round, and stop on any that fails rather than discovering it at wrap-up.

1. **The tree is clean.** The loop commits the edits of each round, so uncommitted work to tracked files can go into those commits, and the report cannot separate it. Show `git status` and stop, unless `--allow-dirty` says the user accepts it. With `--allow-dirty`, each round commit takes only the files that the round edited.

1. **The ticket is known.** Get the ticket key from the branch name, as `.claude/rules/commit.md` says. The round commits use it. When the branch name has no key, use the key of the last commits, or ask the user before the first round.

1. **The diff is non-empty.** `git diff --name-only "${merge_base}"..HEAD` returns something, or there is nothing to review. Say "Nothing to review." and stop.

## The Round

### Run The Reviewers

They are independent and read the same diff, so dispatch them in one turn:

- **`/code-review ${effort} ${target}`**, through the `Agent` tool with `subagent_type: general-purpose` and `model: sonnet`, so that the review runs on the cheaper model. Tell the agent to invoke the `code-review` skill through the `Skill` tool with the arguments `${effort} ${target}`, to edit no file, and to return every finding as the skill reports it, with its file, line, summary, and verdict. Each finding carries a `CONFIRMED` or `PLAUSIBLE` verdict — `PLAUSIBLE` is a lead to verify, never a verdict to apply. When the agent cannot run the skill, invoke `/code-review` through the `Skill` tool in this context instead, and say so in the report.

- **the `custom-pr-reviewer` and `custom-pr-reviewer-naming` agents**, through the `Agent` tool, in the same turn. They count as one reviewer. `custom-pr-reviewer` covers ordering, idiom, and the functional vetoes. `custom-pr-reviewer-naming` covers only the names, and runs on a stronger model. Each one returns one report without editing anything, and they keep N rounds of full reports out of this loop's context. When an agent type is unavailable, continue the round with the other reviewers, and report which agent did not run.

- **the pr-reviewer**, through the `Bash` tool in the foreground, with a timeout of 600000 ms. It checks the diff against the rules in `pr-reviewer/rules` that the source formatter does not enforce. It ignores generated files, frontend sources, generated locale files, and lock files. Find it the way the pr-check validation `pr-reviewer.md` does:

	```bash
	REPO_ROOT=$(git rev-parse --show-toplevel)
	REVIEWER_HOME="${REPO_ROOT}/pr-reviewer"

	if [[ ! -f ${REVIEWER_HOME}/review.sh ]]
	then
		REVIEWER_HOME="${LIFERAY_PR_REVIEWER_HOME:-${REPO_ROOT%/*}/liferay-portal/pr-reviewer}"
	fi

	LOG_DIR="$(git rev-parse --absolute-git-dir)/code-review-loop"
	LOG="${LOG_DIR}/pr-reviewer-round-${round}.json"
	LOG_ERROR="${LOG_DIR}/pr-reviewer-round-${round}.err"

	mkdir -p "${LOG_DIR}"

	(cd "${REPO_ROOT}" && "${REVIEWER_HOME}/review.sh" --json "${merge_base}..HEAD" > "${LOG}" 2> "${LOG_ERROR}")
	```

	The exit status gives the result:

	| Exit | Meaning | Action |
	| --- | --- | --- |
	| `0`, `reviewed_files` is not empty | No violation | No finding from this reviewer |
	| `0`, `reviewed_files` is empty | Each changed file is generated, frontend, or ignored | No finding. Say so in the summary |
	| `1` | Violations | Each item of the `violations` array is one finding, with `file`, `line`, `rule`, `message`, and `fix` |
	| `2` | The reviewer did not run, for example because `claude` is not on the `PATH` | Continue the round with the other two reviewers. Quote `${LOG_ERROR}` in the summary |

	With exit `0` or `1`, `${LOG_ERROR}` can name a rule group that gave no verdict. Quote it in the summary, because that group did not review the round.

	When `${REVIEWER_HOME}/review.sh` does not exist, do not stop the loop. Run the other two reviewers, and say in the summary which path you tried. Tell the user to set `LIFERAY_PR_REVIEWER_HOME` or to check out `liferay-portal` next to this repository.

**Never pass `--fix` or `--comment` to either.** A reviewer that edits the tree hides what it changed from the ledger, and one that comments publishes a finding this loop has not triaged yet.

### Normalize And Deduplicate

Give every finding a fingerprint: `<path>::<symbol or rule number>::<claim slug>`. Never key on the line number — the previous round's fixes move it, and the same opinion then reads as a new finding round after round.

For a pr-reviewer violation, use the `rule` field as the rule number, and make the claim slug from `message`.

Two reports reaching the same fingerprint are **one** finding with both sources named, not two rows.

### Drop What The Ledger Settled

A fingerprint already carrying a `Decline` or `Defer` verdict is not re-litigated. A `Skip` verdict is settled only while `--min-severity` stays the same (see **Severity**). Count it as re-raised, note which reviewer raised it again, and move on. This is the mechanism that lets the loop end.

The one exception: a finding is re-opened when the code under it changed since the verdict was written. The reason a verdict was given is part of the ledger precisely so this is a check and not a judgment call.

### Verify Before Judging

Every survivor is read in the file before it earns a verdict. Three rules, each answering a measured failure of these reviewers:

- **Agreement is correlated, not confirmation.** The reviewers read the same diff under similar briefs and rules, and they converge on wrong findings reliably on three axes: permission logic called dead, member and method ordering, and "reuse the existing util". Anything a reviewer raises on those axes, and anything two or more raise together, is verified against the code — never promoted because two reports carry it.

- **A correctness claim reasoned from reading is a hypothesis.** Mutation-check it: mutate the code the finding says is wrong and require a test to go red. A mutation that stays green means the defect is not there, or nothing covers it — find out which before paying for a fix.

- **A pr-reviewer `fix` is a proposal.** Read the rule file `${REVIEWER_HOME}/rules/<rule>-*.md` and the code around the line before you give a verdict. A `fix` that starts with `Verify:` is one that the reviewer was not sure about. Treat it as a `PLAUSIBLE` finding.

- **A rule number is only cited from its file.** The binding catalog is `${root}/pr-reviewer/rules/NNN-*.md` plus `STYLE.md`. Never carry a rule number a file does not back.

### Triage

Give every verified finding a severity (see **Severity**), then a class, and write the row into the ledger as you go.

| ID | Finding | Source | Severity | Class | Reason |
| --- | --- | --- | --- | --- | --- |

### Apply And Format

Make every edit the round is going to make. When an edit changed a file in `workspaces/liferay-sample-workspace`, run `workspaces/refresh_other_workspaces.sh` when it exists. The pr-reviewer does not flag the copies that the refresh writes, and without the refresh each copy keeps the old text and fails the pr-check validation Generated Workspace File. **Then** run `format-source` once over the union of the modules touched. Formatting per fix pays for a build and a pass of the formatter's manual rules each time, and half of what the reviewers raise the formatter settles for free.

Then commit the edits of the round, per **Commits** below, and start the next round. The next round reads `${merge_base}..HEAD`, so all three reviewers see the edits of this round.

## The Actionability Bar

This bar is the whole point of the loop: it decides when a finding is worth a round, and it is what makes "the reviewers went quiet" mean something.

**Apply** when all four hold:

- It lands on code this branch adds or changes. Pre-existing code the branch merely sits beside belongs to another ticket.

- It was verified in the file, not taken from the report.

- It names the concrete edit. "This could be clearer" with no replacement offered is an opinion, not an action.

- It cites a rule under `pr-reviewer/rules`, a proven defect, or a missing test for a branch this change introduces.

**Defer** when the finding is real but outside this ticket. Name the follow-up ticket, or say plainly that none exists yet.

**Decline** on any of:

- The file is generated — `@generated`, `@Generated("")`, `$ANTLR`, `module-hbm.xml`, `portlet-model-hints.xml`, the `sql` directory, or anything under `*-rest-api`/`*-rest-client` that is not a hand-written `rest-*.yaml` or `*Impl`/`*Test` body. Regenerate; never hand-edit.

- The formatter owns it, and `formatSource` settles it either way. This includes each pr-reviewer `fix` that contains `SourceFormatter is authoritative`.

- **Rule 001 overrides it** — matching the file's or the siblings' existing convention is what breaks the general rule. The local pattern wins, and mirroring it is the correct code.

- The mutation check came back green, so the defect does not exist.

- It is taste with no rule and no defect behind it, or a rewrite of code the change did not touch.

Every `Decline` carries a reason grounded in the code or in a rule — never a silent drop, and never a bare disagreement. **The reason is what stops the next round re-opening it**, so a weak reason costs a round.

**Skip** when the finding passes the four `Apply` conditions, but its severity is lower than `--min-severity`. The reason is `Below --min-severity <level>`. Do not make the edit.

## Severity

Give each finding one severity. The loop sets it after it reads the code. Do not copy the label of the reviewer, because the reviewers use different scales. When two sources report the same finding, use the highest severity that the code supports.

| Severity | Use it when the finding is | Typical source |
| --- | --- | --- |
| **Critical** | A proven defect that breaks the feature, loses data, removes a permission check, opens a security gap, or breaks the build or a test | code-review `CONFIRMED`, a red mutation check |
| **High** | A defect that is probable but not proven, a missing test for a branch that this change adds, or a finding that makes the reviewer reject the pull request | code-review `PLAUSIBLE` on correctness, custom-pr-reviewer `BLOCK` |
| **Medium** | A violation of a rule in `pr-reviewer/rules` that does not change the behavior | pr-reviewer, custom-pr-reviewer `WARN` |
| **Low** | A small improvement of readability or naming with a concrete edit and no rule violation | custom-pr-reviewer `NIT`, code-review `nit` |

Give a severity also to a finding that you decline or defer: it is the severity of the claim if the claim were true. The report uses it to sort the rows.

The order is `critical` > `high` > `medium` > `low`. With `--min-severity high`, the loop applies `critical` and `high`, and gives `Skip` to `medium` and `low` findings that pass the bar.

A `Skip` verdict is not final. On entry, re-triage each `Skip` row of the ledger against the current `--min-severity`. A lower threshold changes it to `Apply`. `Decline` and `Defer` do not depend on the threshold, so they stay settled.

## Stopping

The loop ends on the first condition that fires.

| Stop | Condition | What Is Reported |
| --- | --- | --- |
| **Converged** | A round produced zero `Apply` — every finding landed in `Decline`, `Defer`, `Skip`, or the ledger | The intended end. Say how many findings the last round still returned, and that they were all settled. Say how many were skipped because of `--min-severity` |
| **Round cap** | `--rounds` reached with work still classified `Apply` | Everything still open, so the user can raise the cap or take it from here |
| **Oscillation** | A fix from round N is reversed by a finding in round N+1 | Both findings, both sources, and the code in between. The loop does not flip a decision back and forth; the user settles it |
| **No progress** | A fingerprint applied in round N comes back open in round N+1 | The fix did not land. Show the edit and the finding rather than applying it again |
| **Broken tree** | `formatSource` fails, a touched module stops compiling, or the round commit fails | Stop immediately. A broken tree is not a state to iterate on. Say which edits are not committed |

## Commits

At the end of each round that applied at least one change, make one commit with all the edits of the round. A round with no edit makes no commit.

1. Stage only the files that the round edited, also the files that `format-source` and `refresh_other_workspaces.sh` changed. Name each path. Do not use `git add -A` or `git add .`, because they can take work that is not from this run.

1. Commit with this title and no body:

	```bash
	git commit --message "${ticket} Code review changes"
	```

1. Write the short hash of the commit into the **Change** column of each `Apply` row of the round.

Rules for the commits:

- Make a new commit for each round. Do not amend, fixup, squash, rebase, stash, or push. The user decides later how to fold the round commits into the commits of the branch.
- When a hook blocks the commit, stop the loop with the stop condition **Broken tree**, and quote the hook output. Do not go around the hook.
- When the commit fails for another reason, stop the loop the same way. Do not start a round on edits that are not committed.

## The Ledger

The run can span sessions, and the scratchpad is session-scoped and gets cleaned, so the ledger lives in the worktree's own git directory, which is durable, per-worktree, and impossible to commit:

```bash
mkdir -p "$(git rev-parse --absolute-git-dir)/code-review-loop"
```

The file is `$(git rev-parse --absolute-git-dir)/code-review-loop/<branch>-ledger.md`, updated as each verdict is decided rather than at the end of the round. One row per fingerprint:

| ID | Fingerprint | Round | Sources | Location | Severity | Finding | Class | Reason | Change |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

- **ID** is the severity prefix and a number: `CRIT1`, `HIGH1`, `MED1`, `LOW1`. Each severity has its own counter, in the order of discovery. Give the ID when the finding gets its severity. The ID never changes, and the final report uses it, so the user can find each row here. A re-opened finding keeps its ID and its severity.
- When a row of an earlier run has an `F` ID, such as `F4`, give it the new ID on entry, and keep the old ID in parentheses in the **Fingerprint** column.
- **Location** is `path:line` at the time of the verdict.
- **Severity** is `critical`, `high`, `medium`, or `low`. When a row of an earlier run has no severity, give it one on entry and write it into the row.
- **Finding** is the claim in one plain sentence, not the reviewer's text.
- **Change** is, for `Apply`, the edit that was made: the files and lines, what changed, and the short hash of the round commit. For the other classes, it is `—`.

On entry, read it when it exists for this branch and carry its verdicts in — that is what makes a second invocation cheap instead of a replay. `--fresh` drops it.

## Guardrails
- **Only this ticket.** A finding about a neighbouring ticket's code is a `Defer`, never scope to absorb.

- **Never hand-edit a generated file.** Re-run the generator per `.claude/rules/service-builder.md`, `.claude/rules/rest-builder.md`, or `.claude/rules/language.md`. Put the regenerated output in the round commit, like the other edits.

- **A finding is not a fact.** Nothing is applied that was not read in the file first, and nothing is applied because two reports agreed.

- **The reviewers never edit.** No `--fix`, no `--comment`, in any round.

## Final Report

Close the run with one report. The user reads this report, not the transcript, so each finding must be clear without the transcript. Write it in the language of the conversation. Build it from the ledger, and use the ledger IDs, so each row is traceable.

Rules for the report:

- Describe each finding as a problem in the code, in one plain sentence. Do not paste the reviewer's text, and do not use a fingerprint or a rule number as the description.
- Give each finding a clickable `path:line`, and the rule file or the test that backs it, when one exists.
- Give each finding exactly one status from this list, and use the same words in every table:

	| Status | Meaning |
	| --- | --- |
	| **Applied** | The change is in the tree |
	| **Declined** | Not a real problem, or not for this branch. The reason says which |
	| **Deferred** | A real problem outside this ticket. The reason names the follow-up |
	| **Skipped** | A real problem for this branch, but its severity is lower than `--min-severity` |
	| **Open** | Still waiting for a decision: round cap, oscillation, or no progress |

- The terminal does not show text colors, so the icons are the color code. Use only these icons, and use them the same way in every table:

	| Severity | Icon | ID prefix |
	| --- | --- | --- |
	| **Critical** | 🔴 | `CRIT` |
	| **High** | 🟠 | `HIGH` |
	| **Medium** | 🟡 | `MED` |
	| **Low** | 🔵 | `LOW` |

	| Status | Icon |
	| --- | --- |
	| **Applied** | ✅ |
	| **Declined** | 🚫 |
	| **Deferred** | ⏩ |
	| **Skipped** | ⏬ |
	| **Open** | ⚠️ |

- Put the severity icon before the ID, for example `🔴 CRIT1`. The ID gives the severity, so do not add a **Severity** column.
- Put the status icon before the status word, for example `🚫 Declined`.
- Start the title with one icon for the stop condition: ✅ **Converged**, ⏱️ **Round cap**, 🔁 **Oscillation**, ⛔ **No progress**, or 💥 **Broken tree**.
- Sort the rows of each table by severity, from **Critical** to **Low**. Inside one severity, sort by the number of the ID.
- In the severity bars, give each finding one block `█`, up to 20 blocks. When a severity has more than 20 findings, scale all bars to the largest count. Pad each bar with `░` to the same length.
- Show every finding of this run, also the ones from the ledger of an earlier run. Mark those with `(earlier run)`.
- Keep each reason to one sentence. Make it about the code or the rule, not about the reviewer.
- Do not add a table that has no rows. Write one line instead, for example "No finding was declined."

Use this structure:

```markdown
## ✅ Code review loop: <one-line result>

**Result:** <stop condition in plain words, for example "Converged in round 3: the last round returned 4 findings, all already decided.">
**Scope:** <N> files, base `<merge_base short>`, effort `<level>`, minimum severity `<level>`, <R> rounds.
**Commits:** <one short hash for each round that applied a change, for example "`a1b2c3d` (round 1), `e4f5a6b` (round 2)">, or "None".

### Summary

```text
🔴 Critical  ██░░░░░░░░   2
🟠 High      ████░░░░░░   4
🟡 Medium    ██████████  10
🔵 Low       ███░░░░░░░   3
```

| | ✅ Applied | 🚫 Declined | ⏩ Deferred | ⏬ Skipped | ⚠️ Open | Total |
| --- | --- | --- | --- | --- | --- | --- |
| 🔴 **Critical** | 2 | 0 | 0 | 0 | 0 | **2** |
| 🟠 **High** | 2 | 1 | 1 | 0 | 0 | **4** |
| 🟡 **Medium** | 6 | 4 | 0 | 0 | 0 | **10** |
| 🔵 **Low** | 0 | 1 | 0 | 2 | 0 | **3** |
| **Total** | **10** | **6** | **1** | **2** | **0** | **19** |

### ✅ Changes applied

| ID | Problem | Where | Found by | Change made |
| --- | --- | --- | --- | --- |
| 🔴 CRIT1 | The method returns `null` when the group has no layouts. | `LayoutUtil.java:88` | code-review | Return an empty list. Test added in `LayoutUtilTest.java:41`. |

### Findings not applied

| ID | Problem | Where | Found by | Status | Reason |
| --- | --- | --- | --- | --- | --- |
| 🟠 HIGH2 | The upgrade process does not clear the cache. | `UpgradeFoo.java:55` | code-review | ⏩ Deferred | Outside this ticket. No follow-up ticket exists yet. |
| 🟡 MED4 | The fields are not in alphabetical order. | `FooImpl.java:20` | custom-pr-reviewer, pr-reviewer | 🚫 Declined | Rule 001: the sibling classes use the same order. |
| 🔵 LOW3 | The variable name `list` does not say what it holds. | `FooImpl.java:42` | custom-pr-reviewer | ⏬ Skipped | Below `--min-severity medium`. |

### Rounds

| Round | Found | New | Applied | Declined | Deferred | Skipped | Repeated from ledger |
| --- | --- | --- | --- | --- | --- | --- | --- |

### Reviewer problems

<Each round in which a reviewer did not run, or a pr-reviewer rule group gave no verdict, with the error text. Omit the section when there is none.>

### ⚠️ What is left

<Each Open and Deferred finding by ID, with its icons, sorted by severity, with the decision that the user must make. When findings were skipped, give the `--min-severity` value that applies them. When the run made commits, say that the user can fold the `Code review changes` commits into the commits of the branch before the pull request. Then the single next step.>

Ledger: `<absolute path of the ledger>`. To change a decision, reply with the ID, for example "apply MED4".
```

When the run was long enough that the user has likely walked away, send the same outcome over Telegram — short, outcome first: rounds run, stop condition, findings applied.
