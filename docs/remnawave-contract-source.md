# Remnawave contract lock

## Current compatibility target

- Remnawave Panel / Backend: **3.4.8** (latest stable release checked on 2026-11-08).
- Matching official `@remnawave/backend-contract`: **3.4.15**.
- Official release: <https://github.com/remnawave/backend/releases/tag/3.4.8>.
- Backend release commit: `4e989707a20785ea7d445ddb1e09132d800f48d8`.
- The contract version is verified from the release's own
  [`libs/contract/package.json`](https://github.com/remnawave/backend/blob/3.4.8/libs/contract/package.json),
  not inferred from an npm `latest` tag. The documentation SDK table still lists
  Panel 3.4.4; the 3.4.8 source also declares contract 3.4.15.

## Official OpenAPI acquisition

- Source: <https://cdn.docs.rw/docs/openapi.json>.
- Retrieved document: `info.version: 3.4.8`, `title: Remnawave API v3.4.8`.
- The release's official
  [OpenAPI publishing workflow](https://github.com/remnawave/backend/blob/3.4.8/.github/workflows/push-openapi-specs.yml)
  generates `openapi.json` on release tags and publishes it under `docs/`.
- Original downloaded JSON SHA-256:
  `b8c1480ab310da0af0f802f8d59b17766e6097ad5b5d6b83eb0bb9829e83b89b`.
- Formatted repository JSON SHA-256 (UTF-8, two-space indentation, LF):
  `ff6edd22f333335478a8f714457b950e16dd56a4fdc2096192c7ddee4bd666ff`.
- Counts: **162 paths, 218 operations, 309 schemas**.

The repository copy is `docs/remnawave-openapi.json`. Machine-readable version,
source, checksum and canonical paths/components fingerprints are recorded in
[`remnawave-contract-lock.json`](remnawave-contract-lock.json). LF normalization makes the formatted
checksum independent of a Windows Git checkout's CRLF conversion.

## Changes from the previous target

The previous repository baseline was Panel 3.4.4 / contract 3.4.15 at bot commit
`9184031efe393717e9dc85f61bfe9f9ffe636d8c`. Comparing the complete JSON documents:

- Only `info.title` and `info.version` changed.
- All `paths` and all `components`, including request and response schemas,
  are identical, not merely the endpoints used by this bot.
- `openapi`, `tags` and `servers` are also unchanged.
- No runtime route, payload field, response envelope or user identity change is
  required for 3.4.8. The bot continues to use numeric Panel user IDs.

See [3.4.4 to 3.4.8 verification](remnawave-3.4.8-compatibility.md) and the
[complete runtime API inventory](remnawave-api-inventory.md).

The public CDN URL can change to a later Panel release. Always inspect its
reported version before refreshing the vendored file. Never relabel an older
specification as a new release. Any target change must update the official
contract snapshot, runtime client, inventory, lock and contract tests together.

These are official-source and offline contract checks, not a claim that a live
Panel or production Telegram bot has been exercised.
