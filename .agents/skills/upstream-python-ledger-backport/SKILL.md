---
name: upstream-python-ledger-backport
description: "Keep the self-hosted Python ledger and nvgrw/improvements patch stack current with main@upstream. Use when reviewing upstream changes, rebasing or backporting upstream into the maintained Jujutsu stack, checking ledger OpenAPI parity, adapting backend-cluster/ledger-python, resolving mobile deletion conflicts, or removing obsolete TypeScript-ledger patches."
argument-hint: "[upstream bookmark, default: main@upstream]"
---

# Upstream Python Ledger Backport

Keep `nvgrw/improvements` as a minimal, dependency-ordered patch stack over upstream while preserving the Python ledger as the production implementation of the canonical ledger API.

Use `jj --no-pager` for every Jujutsu command. Never mutate the real stack until the read-only audit and a disposable rebase agree on the required work.

## Invariants

- `main@upstream` is the upstream base; `main@origin` may lag and is only a comparison point.
- `nvgrw/improvements` contains self-hosted patches, not copies of behavior already supplied by upstream.
- `backend-cluster/ledger-python` implements every method/path pair in `backend-cluster/idl/beancount-ledger.openapi.json`.
- Semantic API growth matters even when upstream touches no Python files.
- `cli/src/bea_engine` and `cli/src/fava` are Python-ledger runtime dependencies copied by its Dockerfile. Keep them unless the runtime is moved first. CLI commands and generated CLI clients are not automatically part of a server backport.
- Production uses the Python ledger. Drop patches that only adapt the superseded TypeScript ledger when Python already implements the behavior.
- `mobile/` stays absent from the self-hosted branch, including files newly added upstream.
- Never leave divergent change IDs as a backup mechanism. Preserve recovery with the operation log and by duplicating revisions before rebasing.
- Do not push or deploy unless the user explicitly asks.

## 1. Establish The Baseline

```zsh
jj --no-pager status
jj --no-pager op log --limit 3
jj --no-pager bookmark list main nvgrw/improvements
jj --no-pager git remote list
jj --no-pager git fetch -b main --remote=upstream
```

STOP if the working copy has unfamiliar changes. Preserve user edits before rewriting history.

Record the current head, tree, and aggregate patch hash:

```zsh
old_head=$(jj --no-pager log -r nvgrw/improvements --no-graph -T 'commit_id')
old_base=$(jj --no-pager log \
  -r 'roots(main@upstream..nvgrw/improvements)-' \
  --no-graph -T 'commit_id')
git rev-parse "$old_head^{tree}"
jj --no-pager diff --from "$old_base" --to nvgrw/improvements --git | shasum -a 256
```

## 2. Audit Upstream Semantically

List commits and changed paths:

```zsh
jj --no-pager log -r 'main@origin..main@upstream' --reversed --no-graph \
  -T 'commit_id.short() ++ " " ++ description.first_line() ++ "\n"'
git diff --name-status \
  "$(jj --no-pager log -r main@origin --no-graph -T 'commit_id')" \
  "$(jj --no-pager log -r main@upstream --no-graph -T 'commit_id')"
```

Build a per-patch path-overlap matrix, but treat it only as routing evidence. A Python replacement commonly has zero textual overlap with upstream TypeScript changes while still falling behind the shared API contract.

Compare the canonical ledger operations before and after upstream:

```zsh
base=$(jj --no-pager log -r main@origin --no-graph -T 'commit_id')
upstream=$(jj --no-pager log -r main@upstream --no-graph -T 'commit_id')
mkdir -p .agents/tmp
for revision in "$base" "$upstream"; do
  git show "${revision}:backend-cluster/idl/beancount-ledger.openapi.json" |
    jq -r '.paths | to_entries[] | .key as $path | .value | to_entries[] |
      select(.key | IN("get", "post", "put", "delete", "patch")) |
      [.value.operationId, (.key | ascii_upcase), $path] | @tsv' |
    sort > ".agents/tmp/ledger-operations-${revision}.txt"
done
comm -13 ".agents/tmp/ledger-operations-${base}.txt" ".agents/tmp/ledger-operations-${upstream}.txt"
comm -23 ".agents/tmp/ledger-operations-${base}.txt" ".agents/tmp/ledger-operations-${upstream}.txt"
```

For each added or changed operation, trace all four surfaces:

1. canonical ledger OpenAPI schema;
2. TypeScript ledger semantics and tests;
3. backend-v2 REST/GraphQL/MCP adapters and authorization;
4. dashboard consumers and generated types.

Backport observable server behavior, not implementation language. Include response aliases, cache invalidation, write boundaries, error behavior, and UI-required metadata.

## 3. Simulate The Rebase

Use a non-integrated operation first:

```zsh
jj --no-pager rebase --no-integrate-operation \
  -s 'roots(main@upstream..nvgrw/improvements)' \
  -d main@upstream
```

Inspect the returned operation without integrating it:

```zsh
jj --no-pager --at-operation <operation-id> log \
  -r 'main@upstream..nvgrw/improvements' --reversed
jj --no-pager --at-operation <operation-id> resolve --list
```

Classify each maintained patch:

- **keep unchanged**: still independent and behaviorally valid;
- **update**: shared contract or behavior changed;
- **drop**: upstream made it redundant, or it only changes a superseded implementation;
- **resolve mechanically**: for example, retain deletion of upstream-modified mobile files.

## 4. Create Fresh Patch Identities

Do not rebase the same change IDs while the remote or a safety bookmark retains their old commits. Duplicate the connected stack directly onto upstream:

```zsh
jj --no-pager duplicate \
  -r 'main@upstream..nvgrw/improvements' \
  --onto main@upstream
```

Record the duplicate mapping printed by Jujutsu. Keep `nvgrw/improvements` on the old head until the duplicate stack is validated. This makes the old remote stack the recovery point without creating divergent change IDs.

If patches must be dropped, abandon only their duplicated revisions. If an obsolete TypeScript-ledger patch contains behavior Python still needs, first move that behavior into the duplicated Python-ledger patch, validate it, then abandon the obsolete patch.

## 5. Update The Python Ledger Patch

Edit the duplicated revision by its new change or commit ID:

```zsh
jj --no-pager edit <duplicated-python-ledger-revision>
```

Prefer thin adapters over duplicated engines:

- call `bea_engine` for managed loading, feeds, and cache semantics;
- call vendored Fava modules for Fava-compatible reports and serialization;
- keep repository plugins running from activated repository/dependency roots;
- invalidate parsed-ledger cache entries when upstream state changes outside Git;
- expose canonical JSON field names exactly;
- reject writes to virtual or managed sources before any Gitea commit.

Add or update `test_routes_match_canonical_ledger_contract` whenever the OpenAPI contract changes. It must compare normalized method/path pairs, not a hard-coded operation count.

After the first edit, run the narrowest relevant Python test immediately. Then run the complete Python suite before moving the branch bookmark.

## 6. Keep Mobile Removed

Resolve upstream modify/delete conflicts by retaining deletion. Also detect newly added files that never existed when the original removal patch was written:

```zsh
head=$(jj --no-pager log -r <duplicated-stack-head> --no-graph -T 'commit_id')
git ls-tree -r --name-only "$head" mobile
```

The command must print nothing. Add any surviving upstream files to the duplicated `Remove mobile client` patch.

## 7. Validation Gate

Run all applicable checks before moving `nvgrw/improvements`:

1. Python ledger focused tests for each changed behavior.
2. Full Python ledger suite under Python 3.12.
3. Exact normalized method/path parity with the current ledger OpenAPI file.
4. Focused backend-v2 parity/service tests plus backend typecheck.
5. Focused dashboard consumer tests plus dashboard typecheck.
6. Production Linux AMD64 ledger image build and import/route smoke test.
7. `git diff --check` across `main@upstream..new-head`.
8. Jujutsu integrity:

```zsh
jj --no-pager log -r 'divergent()'
jj --no-pager log -r '<new-stack> & conflicts()'
jj --no-pager log -r '<new-stack> & empty()'
```

All three commands must produce no revisions. Confirm unrelated files are byte-identical to upstream and compare the duplicate head tree against the intended reviewed result.

## 8. Activate And Clean Up

Move the bookmark only after validation:

```zsh
jj --no-pager bookmark set --allow-backwards nvgrw/improvements -r <new-head>
```

Abandon the temporary rewritten chain only after the bookmark and working copy reference the fresh-ID duplicate stack. Keep remote commits and operation-log recovery intact. Finish with an empty working-copy child:

```zsh
jj --no-pager new nvgrw/improvements
jj --no-pager status
```

Report the new patch order, dropped/updated patches, validation results, and whether the remote or deployment was changed.

## Stop Conditions

STOP and ask one narrow question when:

- upstream and self-hosted semantics genuinely conflict;
- an operation has no clear canonical behavior or consumer contract;
- preserving a CLI/runtime module would require moving ownership across packages;
- unrelated user changes cannot be isolated safely;
- a required validation environment or credential is unavailable;
- tests expose behavior outside the intended backport scope;
- the proposed rewrite would lose a patch or content not recoverable from the operation log or remote.
