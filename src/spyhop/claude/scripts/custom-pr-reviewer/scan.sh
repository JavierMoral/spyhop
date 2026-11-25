#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

#
# Usage:
#
#   scan.sh [<base> [<head>]]
#
#     Prints one tab separated candidate per line (CHECK, file:line, excerpt)
#     for the lines the diff between <base> and <head> adds. <base> defaults to
#     the merge base with origin/master and <head> to HEAD. Pass "WORKTREE" as
#     <head> to scan uncommitted changes.
#
#   scan.sh precedent <perl-regex> [<pathspec>...]
#
#     Counts the production (non test) files whose content matches the
#     multi-line Perl regex, and prints up to five of them. Use it to prove
#     whether a construct has precedent in the codebase before calling it a
#     convention or an oddity.
#

function main {
	cd "$(git rev-parse --show-toplevel)"

	if [[ ${1:-} == "precedent" ]]
	then
		shift

		_precedent "${@}"

		return
	fi

	local base=${1:-$(git merge-base origin/master HEAD)}
	local head=${2:-HEAD}

	local diff_args=("${base}")

	if [[ ${head} != "WORKTREE" ]]
	then
		diff_args+=("${head}")
	fi

	git diff --no-renames --unified=0 "${diff_args[@]}" | perl "$(dirname "$(readlink --canonicalize "${BASH_SOURCE[0]}")")/scan.pl" "${head}"
}

function _precedent {
	local regex=${1}

	shift

	local pathspec=("${@}")

	if [[ ${#pathspec[@]} -eq 0 ]]
	then
		pathspec=("*.java")
	fi

	git ls-files -- "${pathspec[@]}" \
		| command grep --extended-regexp --invert-match '/(test|testIntegration)/|\.test\.[jt]sx?$' \
		| xargs --no-run-if-empty perl -0 -ne "print \"\${ARGV}\\n\" if /${regex}/s" \
		| sort --unique \
		| awk '{ files[NR] = $0 } END { print NR " production files match"; for (i = 1; i <= NR && i <= 5; i++) print "  " files[i] }'
}

main "${@}"
