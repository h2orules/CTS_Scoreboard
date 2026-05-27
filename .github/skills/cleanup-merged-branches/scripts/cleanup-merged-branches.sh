#!/usr/bin/env bash
# Cleanup local git branches whose remote PR branches have been merged
# and pruned from origin.
#
# Usage:
#   cleanup-merged-branches.sh           # dry run: list candidates
#   cleanup-merged-branches.sh --delete  # actually delete them
#
# A candidate branch must satisfy BOTH:
#   1. Its upstream is gone (remote tracking branch deleted on origin)
#   2. It is fully merged into origin/<default-branch>
#
# Deletion uses `git branch -d` (safe). Never force-deletes.

set -euo pipefail

DELETE=0
if [[ "${1:-}" == "--delete" ]]; then
    DELETE=1
elif [[ -n "${1:-}" ]]; then
    echo "Usage: $0 [--delete]" >&2
    exit 2
fi

# Must run inside a git repo
git rev-parse --git-dir >/dev/null 2>&1 || {
    echo "error: not a git repository" >&2
    exit 1
}

# Detect default branch from origin/HEAD; fall back to master/main.
detect_default_branch() {
    local ref
    ref=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
    if [[ -n "$ref" ]]; then
        echo "${ref#origin/}"
        return
    fi
    for candidate in master main; do
        if git show-ref --verify --quiet "refs/remotes/origin/$candidate"; then
            echo "$candidate"
            return
        fi
    done
    echo "error: could not detect default branch" >&2
    exit 1
}

DEFAULT_BRANCH=$(detect_default_branch)
echo "Default branch: $DEFAULT_BRANCH"
echo "Fetching and pruning origin..."
git fetch --prune origin

# Branches whose upstream is gone.
mapfile -t GONE < <(
    git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads \
        | awk '$2 == "[gone]" { print $1 }'
)

if [[ ${#GONE[@]} -eq 0 ]]; then
    echo "No branches with a gone upstream. Nothing to do."
    exit 0
fi

# Branches fully merged into origin/<default>.
mapfile -t MERGED < <(
    git branch --merged "origin/$DEFAULT_BRANCH" --format='%(refname:short)'
)

# Intersection: gone AND merged, excluding the default branch itself.
CANDIDATES=()
UNMERGED=()
for b in "${GONE[@]}"; do
    [[ "$b" == "$DEFAULT_BRANCH" ]] && continue
    is_merged=0
    for m in "${MERGED[@]}"; do
        if [[ "$m" == "$b" ]]; then
            is_merged=1
            break
        fi
    done
    if [[ $is_merged -eq 1 ]]; then
        CANDIDATES+=("$b")
    else
        UNMERGED+=("$b")
    fi
done

echo
echo "Candidate branches to delete (gone upstream AND merged into origin/$DEFAULT_BRANCH):"
if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
    echo "  (none)"
else
    printf '  %s\n' "${CANDIDATES[@]}"
fi

if [[ ${#UNMERGED[@]} -gt 0 ]]; then
    echo
    echo "Gone-upstream branches NOT merged into origin/$DEFAULT_BRANCH (skipped, need manual review):"
    printf '  %s\n' "${UNMERGED[@]}"
fi

if [[ $DELETE -eq 0 ]]; then
    echo
    echo "Dry run. Re-run with --delete to remove the candidates."
    exit 0
fi

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
    exit 0
fi

CURRENT=$(git symbolic-ref --short HEAD 2>/dev/null || true)

# If currently on a candidate, switch to default branch first.
for b in "${CANDIDATES[@]}"; do
    if [[ "$b" == "$CURRENT" ]]; then
        echo
        echo "Currently on '$CURRENT' (a deletion candidate); switching to '$DEFAULT_BRANCH'."
        git checkout "$DEFAULT_BRANCH"
        break
    fi
done

# Fast-forward default branch if behind. Required so recent merge commits
# are visible locally; otherwise `git branch -d` reports "not fully merged".
if [[ "$(git symbolic-ref --short HEAD)" == "$DEFAULT_BRANCH" ]]; then
    if ! git merge-base --is-ancestor "origin/$DEFAULT_BRANCH" HEAD; then
        echo "Fast-forwarding '$DEFAULT_BRANCH' to origin/$DEFAULT_BRANCH..."
        git pull --ff-only
    fi
fi

echo
echo "Deleting ${#CANDIDATES[@]} branch(es)..."
FAILED=()
for b in "${CANDIDATES[@]}"; do
    if ! git branch -d "$b"; then
        FAILED+=("$b")
    fi
done

echo
echo "Remaining local branches:"
git branch

if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo
    echo "WARNING: failed to delete (refused as not fully merged):"
    printf '  %s\n' "${FAILED[@]}"
    echo "Review manually before using 'git branch -D'."
    exit 1
fi
