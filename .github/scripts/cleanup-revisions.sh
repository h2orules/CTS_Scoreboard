#!/usr/bin/env bash
# Cleanup script invoked by .github/workflows/azure-cleanup-revisions.yml.
#
# Required env:
#   RG       resource group containing the Container App
#   ACR      registry name (no .azurecr.io suffix)
#   APP      Container App name
#   REPO     image repository name within the ACR (e.g. cts-relay)
#   KEEP     number of most-recent active revisions to keep
#   DRY_RUN  "true" => print actions only; anything else executes
#
# Two-phase cleanup:
#   1. Deactivate every active revision past the KEEP-newest (by
#      createdTime desc). Inactive revisions are left as-is; Container
#      Apps reserves their names permanently regardless.
#   2. Delete any manifest in $ACR/$REPO that isn't referenced (by
#      digest *or* tag) by the surviving active revisions.

set -euo pipefail

: "${RG:?}" "${ACR:?}" "${APP:?}" "${REPO:?}" "${KEEP:?}" "${DRY_RUN:=false}"

log() { printf '[%s] %s\n' "$APP" "$*"; }

run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY-RUN: $*"
  else
    log "RUN: $*"
    "$@"
  fi
}

# Emit machine-readable pending counts for the workflow to pick up.
# Always uses $DEACTIVATE_COUNT (set in phase 1) plus the supplied
# $1 = manifest delete count from phase 2.
emit_outputs() {
  local delete_count="$1"
  local pending=$(( DEACTIVATE_COUNT + delete_count ))
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
      echo "deactivate_count=$DEACTIVATE_COUNT"
      echo "delete_count=$delete_count"
      echo "pending=$pending"
      echo "dry_run=$DRY_RUN"
    } >> "$GITHUB_OUTPUT"
  fi
  log "Summary: deactivate=$DEACTIVATE_COUNT delete=$delete_count pending=$pending dry_run=$DRY_RUN"
}

DEACTIVATE_COUNT=0

# ----------------------------------------------------------------------
# Phase 1: deactivate excess active revisions.
# ----------------------------------------------------------------------
log "Listing active revisions (keep newest $KEEP)..."
ALL_ACTIVE_JSON=$(az containerapp revision list \
  -g "$RG" -n "$APP" \
  --query "[?properties.active].{name:name, image:properties.template.containers[0].image, created:properties.createdTime, traffic:properties.trafficWeight}" \
  -o json)

ACTIVE_COUNT=$(jq 'length' <<<"$ALL_ACTIVE_JSON")
log "Currently $ACTIVE_COUNT active revision(s)."

# Sort newest first, drop the first KEEP. Never deactivate a revision
# that is currently receiving traffic (>0 weight) — that would 503 the
# app even if it's "older" than KEEP cutoff. Such a revision should
# instead be cleaned up by the next deploy.
TO_DEACTIVATE=$(jq -r --argjson keep "$KEEP" '
  sort_by(.created) | reverse
  | .[$keep:]
  | map(select((.traffic // 0) == 0))
  | .[].name
' <<<"$ALL_ACTIVE_JSON")

DEACTIVATE_COUNT=0
if [[ -z "$TO_DEACTIVATE" ]]; then
  log "Nothing to deactivate."
else
  DEACTIVATE_COUNT=$(printf '%s\n' "$TO_DEACTIVATE" | sed '/^$/d' | wc -l | tr -d ' ')
  while read -r rev; do
    [[ -z "$rev" ]] && continue
    run az containerapp revision deactivate \
      -g "$RG" -n "$APP" --revision "$rev"
  done <<<"$TO_DEACTIVATE"
fi

# Also warn (don't block) if any active revision past KEEP is still
# carrying traffic — operator decision required.
KEPT_BY_TRAFFIC=$(jq -r --argjson keep "$KEEP" '
  sort_by(.created) | reverse
  | .[$keep:]
  | map(select((.traffic // 0) > 0))
  | .[].name
' <<<"$ALL_ACTIVE_JSON")
if [[ -n "$KEPT_BY_TRAFFIC" ]]; then
  log "WARNING: these revisions are past the keep cutoff but still have traffic; left active:"
  echo "$KEPT_BY_TRAFFIC" | sed 's/^/  - /'
fi

# ----------------------------------------------------------------------
# Phase 2: prune ACR images not referenced by any surviving active rev.
# ----------------------------------------------------------------------
log "Collecting image references from remaining active revisions..."
REFS_JSON=$(az containerapp revision list \
  -g "$RG" -n "$APP" \
  --query "[?properties.active].properties.template.containers[].image" \
  -o json)

# Each image string is either ${ACR}.azurecr.io/${REPO}@sha256:... or
# ${ACR}.azurecr.io/${REPO}:tag. Build two lookup sets.
REGISTRY_PREFIX="${ACR}.azurecr.io/${REPO}"
REFERENCED_DIGESTS=$(jq -r --arg prefix "$REGISTRY_PREFIX" '
  .[] | select(startswith($prefix + "@")) | sub($prefix + "@"; "")
' <<<"$REFS_JSON" | sort -u)
REFERENCED_TAGS=$(jq -r --arg prefix "$REGISTRY_PREFIX" '
  .[] | select(startswith($prefix + ":")) | sub($prefix + ":"; "")
' <<<"$REFS_JSON" | sort -u)

log "Active revisions reference $(wc -l <<<"$REFERENCED_DIGESTS" | tr -d ' ') digest(s) and $(wc -l <<<"$REFERENCED_TAGS" | tr -d ' ') tag(s)."

# Resolve any referenced tags to digests so digest-based pruning catches
# "same manifest, different tag" cases correctly.
TAG_DIGESTS=""
while read -r tag; do
  [[ -z "$tag" ]] && continue
  d=$(az acr repository show \
        --name "$ACR" --image "$REPO:$tag" \
        --query digest -o tsv 2>/dev/null || true)
  if [[ -n "$d" ]]; then
    TAG_DIGESTS+="$d"$'\n'
  fi
done <<<"$REFERENCED_TAGS"

KEEP_DIGESTS=$(printf '%s\n%s\n' "$REFERENCED_DIGESTS" "$TAG_DIGESTS" | sort -u | sed '/^$/d')
log "Resolved keep-set:"
echo "$KEEP_DIGESTS" | sed 's/^/  + /'

# Enumerate every manifest in the repo. If the repo doesn't exist (fresh
# ACR), bail out cleanly.
if ! az acr repository show --name "$ACR" --repository "$REPO" >/dev/null 2>&1; then
  log "Repository $REPO not present in $ACR; nothing to prune."
  emit_outputs 0
  exit 0
fi

MANIFESTS_JSON=$(az acr manifest list-metadata \
  --registry "$ACR" --name "$REPO" \
  --query "[].{digest:digest, tags:tags}" \
  -o json)

TOTAL=$(jq 'length' <<<"$MANIFESTS_JSON")
log "Repo $REPO contains $TOTAL manifest(s)."

# Build deletion list: any manifest whose digest is not in KEEP_DIGESTS.
# (Tag-only matching isn't enough because deactivated revisions may
# leave dangling tags that still resolve to a digest we want to drop.)
TO_DELETE=$(jq -r --arg keep "$KEEP_DIGESTS" '
  ($keep | split("\n") | map(select(length>0))) as $keep_arr
  | .[] | select(.digest as $d | ($keep_arr | index($d)) == null)
  | .digest
' <<<"$MANIFESTS_JSON")

if [[ -z "$TO_DELETE" ]]; then
  log "No unreferenced manifests; ACR is clean."
  emit_outputs 0
  exit 0
fi

DELETE_COUNT=$(printf '%s\n' "$TO_DELETE" | sed '/^$/d' | wc -l | tr -d ' ')
log "Deleting $DELETE_COUNT unreferenced manifest(s)..."
while read -r digest; do
  [[ -z "$digest" ]] && continue
  run az acr repository delete \
    --name "$ACR" \
    --image "$REPO@$digest" \
    --yes
done <<<"$TO_DELETE"

emit_outputs "$DELETE_COUNT"
log "Done."
