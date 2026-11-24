# RFC 0005 — Bank reconciliation

**Status:** draft

The user uploads a bank statement as CSV, a Celery task reconciles it with remitmatch, and doubtful results go to a review queue. The engine is described in [RFC 0001](0001-remitmatch.md); this RFC covers its integration.

## Flow

1. **Upload.** The user uploads the CSV and chooses the column mapping (date, amount, description, payer). The mapping is saved per bank for next time.
2. **Import** (Celery). It is parsed, normalized to cents, and duplicates are discarded with a hash of `(date, amount, description, account)`. It is idempotent: uploading the same file twice does not duplicate transactions.
3. **Reconciliation** (Celery). The tenant's open invoices are loaded and `reconcile()` is called with the tenant's configuration.
4. **Application.** `auto` matches create `Payment` and update invoice status in one transaction, with audit (actor: system).
5. **Review.** `suggested` and `ambiguous` go to the queue: the user sees the score breakdown and accepts, rejects or reassigns. For ambiguous ones they can ask the assistant for an explanation (RFC 0006).
6. **Undo.** An applied match can be reverted as long as the period is not closed.

## Data model

| Entity | Contents |
| --- | --- |
| `BankStatementImport` | file, mapping, status, counters (read, duplicates, imported) |
| `BankTransaction` | value date, amount, description, payer, deduplication hash |
| `ReconciliationRun` | configuration used, remitmatch version, metrics, duration |
| `MatchProposal` | transactions, invoices, type, status, score and breakdown, user decision |

Storing the library version and the configuration in each run makes it possible to explain later why each match was proposed.

## Metrics shown

- Percentage of automatic, suggested and unmatched payments per run.
- Suggestion acceptance rate: if users accept almost all of them, the `auto` threshold is too high.

## Alternatives considered

| Alternative | Why it was rejected |
| --- | --- |
| Reconcile within the HTTP request | With thousands of transactions it exceeds the Cloud Run timeout |
| Real-time bank connection (Plaid) | Out of v1; the CSV is enough for the demo |
| Apply everything automatically | A false positive marks an invoice as paid; review is mandatory for doubtful cases |
