# Codex project guidance

## Project purpose

This repository contains the internal shipment collaboration system for 万物香铺. Store staff create shipment and return requests; headquarters manages fulfillment, electronic labels, tracking, exports, product data, stores, backups, and operational diagnostics.

Treat the repository as a production system that contains code paths for personal shipping data. Keep changes narrow, auditable, and reversible.

## Durable context

Read these files before changing code:

1. `docs/PROJECT_CONTEXT.md` for product scope and invariants.
2. `docs/ARCHITECTURE.md` for module and data-flow boundaries.
3. `docs/STATUS.md` for the current verified baseline and open verification items.
4. `README.md` for local operation, deployment, integrations, and backup commands.
5. `OPERATIONS.md` for production safety, recovery, capacity, and incident procedures.

Record architectural or operational decisions in `docs/decisions/`. Update the relevant feature entry in `docs/features/README.md` when behavior changes.

## Technical shape

- Backend: Python standard-library HTTP server with a fixed request thread pool.
- Database: SQLite in WAL mode.
- Frontend: native HTML, CSS, and JavaScript in `static/`.
- External integrations: 快递100 tracking and electronic-label APIs.
- PDF work: `pypdf`, isolated in a short-lived subprocess for batch merging.
- Production: Render Web Service with a persistent disk mounted at `/var/data`.

Keep the current simple architecture unless a requested change has evidence that a broader migration is needed.

## Change workflow

- One change request should use one Codex task, one Git worktree, and one branch.
- Start feature, fix, UX, and documentation branches from the latest `main`.
- Use branch prefixes such as `agent/`, `feat/`, `fix/`, `ops/`, or `docs/`.
- Do not develop new features directly in the long-lived project-control or production-operations tasks.
- Do not mix unrelated cleanup into a scoped change.
- Before handoff, summarize changed files, behavior, validation, deployment impact, and unresolved risks.

Use `docs/TASK_TEMPLATE.md` when preparing a new task.

## Required validation

For backend, database, tracking, label, deployment, or cross-cutting changes, run:

```bash
python3 -m py_compile server.py database.py manage.py smoke_test.py tracking.py shipping.py label_pdf.py
node --check static/app.js
python3 smoke_test.py
```

For documentation-only changes, at minimum inspect the diff and verify that all referenced paths and commands still exist. Run the full smoke test when documentation changes executable configuration or claims about current behavior.

For UI changes:

- Check the affected flow in a desktop viewport around 1440 px wide.
- Check the affected flow in a mobile viewport around 390 px wide.
- Test long business IDs, order numbers, names, addresses, remarks, and multi-item orders.
- Confirm browser error logs are clean and interactive controls provide immediate feedback.

## Data and secret safety

- The GitHub repository is public. Never commit production or customer data.
- Never commit `.env`, SQLite databases, WAL/SHM files, downloaded backups, uploaded product workbooks, API secrets, session data, cookies, or credentials.
- Do not print or paste secret environment-variable values into task output, tests, fixtures, screenshots, or documentation.
- Production backups contain recipient names, phone numbers, and addresses. Store them only in controlled locations.
- Do not run destructive tests against production orders, labels, databases, Render services, or persistent disks.
- External writes, production deployment, database restore, service deletion, and environment-variable changes require explicit user authorization for that action.

## Database rules

- Preserve the existing SQLite WAL, busy-timeout, bounded-cache, and online-backup behavior.
- Schema changes must be backward-safe for an existing production database.
- Before a risky migration, create a SQLite online backup and run `PRAGMA integrity_check` on the backup.
- Add migration coverage to `smoke_test.py`, including representative legacy rows.
- Do not replace SQLite merely because the file grows. Use the migration thresholds in `OPERATIONS.md` and re-verify them against current production metrics.
- Never commit a real database to reproduce a bug; create a minimal synthetic fixture.

## Shipment and label invariants

- Staff can access only their own store's records; headquarters can access all stores.
- Frontend permissions are not sufficient. Enforce ownership and shipment-state rules in the database or server layer.
- Once electronic-label ordering has started or a shipment has shipped, disallow unsafe item, remark, or whole-order mutations unless the documented workflow explicitly permits them.
- Label cancellation must reuse the authorization snapshot from the original booking.
- Only clear or unlock a label after the provider confirms success. A local-only cancellation is unsafe because the carrier label may remain valid.
- Rebooking after a confirmed cancellation must use a new request/order identifier while preserving retry idempotency.
- Ordinary 圆通 cancellation may be rejected by the current provider channel. Preserve the existing label and lock state on rejection.
- Keep batch print limits and the isolated PDF merge process unless a measured, tested replacement is introduced.

## Operations rules

- Treat `/api/health` as a lightweight liveness/readability check, not a full diagnostic report.
- Use `/api/admin/system/diagnostics` only with headquarters authorization.
- Keep successful health checks out of noisy access logs.
- Do not run concurrent full tracking syncs or repeated large batch-print requests.
- Verify the exact Render service before any external mutation; historical task context recorded both a production service and an accidental duplicate.
- Separate observation from remediation: an operations task should report evidence, then create a dedicated hotfix task for code changes.

## Documentation truth rules

- Separate verified repository facts from historical production observations.
- Date production observations and name their evidence source.
- If current external state has not been checked, mark it `待核实` instead of presenting old values as current.
- When `README.md`, `OPERATIONS.md`, `render.yaml`, task history, and live production differ, do not silently choose one. Record the discrepancy in `docs/STATUS.md` and verify it in a scoped operations task.

