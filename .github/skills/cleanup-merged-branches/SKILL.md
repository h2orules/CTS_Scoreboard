---
name: cleanup-merged-branches
description: 'Delete local git branches whose remote PR branches have been merged and pruned. Use when the user asks to clean up merged branches, prune stale branches, delete branches for merged PRs, tidy up local branches, or remove old feature branches after merge.'
---

# Cleanup Merged Local Branches

## When to Use

User asks to:
- Clean up local branches associated with merged PRs
- Prune / delete stale local branches
- Remove branches whose upstream has been deleted on origin
- Tidy up `git branch` output after merges

## Procedure

1. Run the automation script to fetch+prune and list candidate branches:

   ```bash
   ./.github/skills/cleanup-merged-branches/scripts/cleanup-merged-branches.sh
   ```

   This is a **dry run**. It prints branches whose upstream is `: gone]` AND
   which are fully merged into `origin/<default-branch>`. Safe to share with
   the user as the proposed delete list.

2. Show the candidate list to the user and confirm deletion (use the
   ask-questions tool when available). Mention any branches that are listed
   as "gone" but NOT merged — those need manual review and won't be deleted.

3. If the user confirms, run the script with `--delete`:

   ```bash
   ./.github/skills/cleanup-merged-branches/scripts/cleanup-merged-branches.sh --delete
   ```

   The script will:
   - Switch off any candidate branch that is currently checked out
     (checks out the default branch first)
   - Fast-forward the local default branch if it is behind origin
     (needed so recent merge commits are visible locally; otherwise
     `git branch -d` refuses with "not fully merged")
   - Delete each candidate with `git branch -d` (safe — never `-D`)
   - Print a final `git branch` summary

4. If `git branch -d` still refuses any branch as "not fully merged",
   STOP and report it to the user. Do not escalate to `-D` without
   explicit confirmation — that branch may have unpushed work.

## Notes

- The script only considers branches with a "gone" upstream. Branches
  that still track a live remote branch are left alone, even if they
  appear merged.
- The default branch is detected from `origin/HEAD`; falls back to
  `master` then `main`.
- The script never force-deletes. All deletions go through `git branch -d`.

## Resources

- [scripts/cleanup-merged-branches.sh](./scripts/cleanup-merged-branches.sh) — automation
