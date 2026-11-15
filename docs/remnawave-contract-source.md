# Remnawave contract lock

## Current compatibility target

- Remnawave Panel / Backend: **3.4.11**, checked on 2026-11-15.
- Matching official `@remnawave/backend-contract`: **3.4.17**.
- Official release: <https://github.com/remnawave/backend/releases/tag/3.4.11> (published 2026-11-14).
- Backend release commit: `6853666801d6d1925b6aec55824106100485777a`.
- Contract package version comes from the release's own
  [`libs/contract/package.json`](https://raw.githubusercontent.com/remnawave/backend/3.4.11/libs/contract/package.json), not an npm latest tag.

## Official OpenAPI acquisition

- Source: <https://cdn.docs.rw/docs/openapi.json>.
- Downloaded document: `info.version: 3.4.11`, `title: Remnawave API v3.4.11`.
- The official [publishing workflow](https://github.com/remnawave/backend/blob/3.4.11/.github/workflows/push-openapi-specs.yml)
  publishes the generated OpenAPI on release tags.
- Raw downloaded JSON SHA-256: `d58d8092dfc3cf979dd3f8cf32df99caa3feff0f86cfae19d852e38d9730a391`.
- Repository JSON SHA-256 (UTF-8, two-space indentation, LF): `48cdd0ca299a3bcce2e9a4e92167b1a354777450b047bb2361dd75c20af93855`.
- Counts: **162 paths, 218 operations, 309 schemas**.

The repository copy is `docs/remnawave-openapi.json`. Version, provenance and
canonical paths/components fingerprints are in [the lock file](remnawave-contract-lock.json).

## Changes from Panel 3.4.8 / contract 3.4.15

- No operation path or method was added or removed.
- The email validation pattern changed in the users stream query and user-related DTOs.
- Host DTOs gained `includeInSrrRules`; Host response schemas require this array.
- The bot does not manage Hosts or validate email with its own copied regex.
  Its existing payload field allowlists and required user response fields still match.
- All 49 direct runtime calls remain mapped to documented operations. Do not add
  guessed routes, HTTP write retries, or unused Host management for this update.

See [3.4.11 compatibility](remnawave-3.4.11-compatibility.md) and
[the runtime inventory](remnawave-api-inventory.md).

The public CDN can advance to another release. Check `info.version` before
updating the snapshot; never relabel an older spec. The contract check and mock
HTTP validation do not establish live Panel or Telegram business compatibility.
