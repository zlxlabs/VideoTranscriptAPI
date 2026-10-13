failure-visibility: clean

# Browser receipt boundary review verdict

- Review verdict: PASS; no findings in the frozen test-only diff.
- Delivery blocking: no; the reviewed change meets the stated receipt-before-read invariant.
- Risk tier: personal. Spec: accepted UI feedback or completed POST must precede `GET /__e2e__/last-upload`.
- Frozen source: `3382ef43047f8961088aca252b6aaca7d934a949..d58ea61e309f6ff94462b302fc59a4bc47538a19`.
- Actual changed scope: `e2e/tests/local-upload.spec.ts:123-135`, 5 additions/5 deletions; only the existing three accepted-UI assertions moved before the receipt GET.
- Manual review: POST reads the request body, inserts its SHA-256/length within SQLite's transaction, exits that transaction, then sends 202; GET reads the newest upload by ROWID. The seeded row is 18 bytes.
- Existing producer and assertions remain: real 46-byte payload, raw length, SHA-256, filename, byte_size, authorization/content/idempotency headers, and decoded metadata.
- OCR: `skipped`, reason `no_reviewable_items`, complete envelope; empty findings are not treated as a clean OCR scan.
- Merged-tree Verify: `uv sync --frozen && npm --prefix e2e ci && npm run test:browser`; exit 0.
- Suite result: 17/17 Chromium passed: full-chain 1, local-upload 7, pages 9. The target receipt test passed.
- Bootstrap: private physical `UV_PROJECT_ENVIRONMENT=<merged-tree>/.venv`; `VIRTUAL_ENV`, `BASH_ENV`, `NODE_OPTIONS`, and `NODE_PATH` unset. `e2e/node_modules` is a physical directory in the temporary merged tree.
- Browser: existing `/mnt/data/cache/ms-playwright` revision `chromium-1234`; Chromium and headless-shell executables both started and reported version `151.0.7922.34`.
- Setup trace: an initial run with the private venv outside the tree exited 1 (16 passed; full-chain could not spawn `<tree>/.venv/bin/python`). The corrected in-tree private venv run above passed; no source change was made for this setup issue.
- Reverse control: with the original assertion order and a temporary 1-second delay before the real POST insert, the actual-byte assertion failed `Expected: 46, Received: 18`.
- Fixed-order control: with that same delay, the target browser test passed. Temporary producer/test edits were removed; both files' blob hashes match their pre-control bytes and the merged tree is clean.
- No product code, fixture, dependency, configuration, API, or test assertion was added or changed beyond the authorized assertion move.
