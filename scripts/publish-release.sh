#!/usr/bin/env bash
# Run under the production-release concurrency group on a trusted main revision.
set -euo pipefail
[[ $VERSION =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ && $ORIGINAL =~ ^[1-9][0-9]*$ ]]
tag=v$VERSION
image=ghcr.io/$(printf '%s' "$GITHUB_REPOSITORY" | tr '[:upper:]' '[:lower:]')
git fetch origin --tags --quiet
newest=$(git tag -l | sed -En '/^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/p' | sort -V | tail -1)
if [[ -n $newest && $(printf '%s\n' "$tag" "$newest" | sort -V | tail -1) != "$tag" ]]; then
  echo 'Newer release exists; refusing channel rollback' >&2
  exit 1
fi

# Only a registry MANIFEST_UNKNOWN is absence. Auth and transport errors abort.
manifest() {
  if skopeo inspect --raw "docker://$image:$1" > "$RUNNER_TEMP/manifest.json" 2>"$RUNNER_TEMP/registry-error"; then
    skopeo manifest-digest "$RUNNER_TEMP/manifest.json"
  elif grep -Eiq 'manifest unknown|MANIFEST_UNKNOWN' "$RUNNER_TEMP/registry-error"; then
    echo missing
  else
    cat "$RUNNER_TEMP/registry-error" >&2
    return 1
  fi
}

docker load --input tested-image.tar
local_image=openruyi-lookout:test
label() { docker image inspect --format "{{ index .Config.Labels \"org.opencontainers.image.$1\" }}" "$local_image"; }
[[ $(label revision) == "$GITHUB_SHA" && $(label version) == "$VERSION" && $(label source) == "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" ]]
image_id=$(docker image inspect --format '{{.Id}}' "$local_image")
[[ $image_id =~ ^sha256:[0-9a-f]{64}$ ]]
sha_tag=sha-$GITHUB_SHA
existing=$(manifest "$sha_tag")
if git show-ref --verify --quiet "refs/tags/$tag"; then
  [[ $(git rev-parse "$tag^{commit}") == "$GITHUB_SHA" ]]
  receipt=$(git tag -l --format='%(contents:subject)' "$tag")
  digest=${receipt#Lookout $BUMP run $ORIGINAL image }
  [[ $receipt == "Lookout $BUMP run $ORIGINAL image $digest" && $digest =~ ^sha256:[0-9a-f]{64}$ && $existing == "$digest" ]]
  [[ $(jq -er '.config.digest' "$RUNNER_TEMP/manifest.json") == "$image_id" ]]
else
  if [[ $existing == missing ]]; then
    docker tag "$local_image" "$image:$sha_tag"
    docker push "$image:$sha_tag"
    existing=$(manifest "$sha_tag")
  fi
  [[ $existing =~ ^sha256:[0-9a-f]{64}$ ]]
  [[ $(jq -er '.config.digest' "$RUNNER_TEMP/manifest.json") == "$image_id" ]]
  digest=$existing
  version_digest=$(manifest "$tag")
  [[ $version_digest == missing || $version_digest == "$digest" ]] || { echo "Conflicting immutable image $tag" >&2; exit 1; }
  git -c user.name=github-actions -c user.email=41898282+github-actions\[bot\]@users.noreply.github.com tag -a "$tag" -m "Lookout $BUMP run $ORIGINAL image $digest" "$GITHUB_SHA"
  git push origin "refs/tags/$tag"
fi

for ref in "$tag" main latest; do
  actual=$(manifest "$ref")
  [[ $actual != "$digest" ]] || continue
  if [[ $ref == "$tag" && $actual != missing ]]; then
    echo "Conflicting immutable image $tag" >&2
    exit 1
  fi
  skopeo copy --preserve-digests "docker://$image@$digest" "docker://$image:$ref"
  [[ $(manifest "$ref") == "$digest" ]]
done

status=$(gh api --paginate "repos/$GITHUB_REPOSITORY/releases?per_page=100" \
  --jq ".[] | select(.tag_name == \"$tag\") | if .draft then \"draft\" else \"public\" end")
case "$status" in
  '') gh release create "$tag" --verify-tag --latest --title "$tag" --notes-file release-notes.md ;;
  draft) gh release edit "$tag" --draft=false --latest ;;
  public) ;;
  *) exit 1 ;;
esac
