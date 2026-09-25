FinanceKit → spend: ingestion plan

Goal: Apple Card, Apple Cash and Savings transactions reach spend's event log automatically. They travel from the iPhone over the tailnet, and no third party other than Apple is in the data path.

Approach: fork LunchSync, strip it down to a FinanceKit ingestor, and make spend the destination. spend becomes a third input next to receipts and bank feeds.
What changes from the original plan

The original plan assumed a mutable Postgres backend with upserts. spend differs in three ways, and each one changes the design.

    Append-only. The phone doesn't "upsert a transaction". It reports an observation of a FinanceKit transaction, and spend stores that observation as an event. Collapsing observations into one current transaction happens in the projection during spend rebuild. You get the revision history (pending $18 → pending $22 → posted $22) automatically, so the separate revisions table is no longer needed.
    Write-only while locked. Ingest has to work the same way the nightly SimpleFIN pull does: encrypt each event to the age public key and write it, without being able to read anything back. So the server can't deduplicate by looking up existing records. It can't report "accepted vs updated", and it can't send webhooks. Deduplication moves into the projection.
    Failures show up as gaps. A FinanceKit event that can't be interpreted produces a failed attempt and no transaction. It never produces a guessed number.

Dropped from the original: the Postgres schema, the FastAPI service, ON CONFLICT upserts, the revisions table, the internal event bus and webhooks, and public HTTPS.

Kept as-is: the entitlement-first approach, the fork and rename, removing Lunch Money and the push server, the DTO boundary, the SwiftData outbox, the rule for ordering history-token saves, background delivery, and deliberate failure testing.
Architecture

┌──────────────────────── iPhone ─────────────────────────┐
│ Apple Wallet → FinanceKit → WalletBridge (LunchSync fork)│
│                                                          │
│   AppleWallet → SwiftData outbox → SyncBroker → SpendAPI │
└───────────────────────────────────────│──────────────────┘
                                        │ HTTPS over tailnet, bearer token
                                        ▼
┌──────────────────────── homelab (spend) ─────────────────┐
│ POST /ingest/financekit                                  │
│      │  validate shape → age-encrypt (public key)        │
│      ▼                                                   │
│ event store (one .age file per event)  ← works locked    │
│      │                                                   │
│  spend unlock → rebuild → SQLite on tmpfs                │
│      │  FinanceKit projection + categories/merchants/    │
│      │  flows rules + receipt matching                   │
│      ▼                                                   │
│ transactions, accounts, line items → UI, agent build     │
└──────────────────────────────────────────────────────────┘

Phase 1: Prove FinanceKit works (do this first)

Fork LunchSync and build it unmodified on a physical iPhone. Before touching anything else, confirm that FinanceKit authorization works, that Apple Card appears, that transactions can be read, and that background delivery fires.

Request the com.apple.developer.financekit entitlement right away. This is the one dependency you can't engineer around. Both the app target and the background-delivery extension need it.

The LunchSync README appears partly stale, because it describes an older APNs-based sync path. Treat the current source code and Apple's FinanceKit Background Delivery docs as the source of truth.
Phase 2: Strip LunchSync down to an ingestor

    Rename the fork (e.g. WalletBridge). Update the bundle IDs, app group, Keychain access group and extension identifiers. Get it compiling, then commit.
    Remove LunchMoneyAPI, the Lunch Money asset pairing, and the lm_id-style fields on Account.
    Remove the old push-server registration.
    Keep AppleWallet, the SwiftData models, SyncBroker and the Keychain helpers.
    Search the repo for leftover integrations:

rg -i "lunchmoney|littlebluebug|register.php|deviceToken|push\."

Exit criterion: the app's only network destination is your spend host. Verify this with Proxyman or Charles rather than assuming it.
Phase 3: Define the event contract

Use four event types, all versioned:

financekit.account.observed
financekit.balance.observed
financekit.transaction.observed
financekit.transaction.removed

A transaction observation looks like this:

{
  "type": "financekit.transaction.observed",
  "schema_version": 1,
  "observed_at": "2026-09-25T15:42:20Z",
  "source": { "app": "walletbridge", "app_version": "1.0", "device": "iphone" },
  "data": {
    "fk_transaction_id": "…",
    "fk_account_id": "…",
    "amount": "27.84",
    "currency": "USD",
    "credit_debit": "debit",
    "foreign_amount": null,
    "foreign_currency": null,
    "status": "pending",
    "transaction_type": "…",
    "merchant_name": "Whole Foods Market",
    "description": "WHOLE FOODS MKT #10234",
    "merchant_category_code": "5411",
    "transaction_date": "2026-09-25T15:42:17Z",
    "posted_date": null
  }
}

Three conventions to follow:

    Store raw values and derive later. Send FinanceKit's positive amount and its credit/debit indicator exactly as given. Send its status enum faithfully rather than collapsing it to pending/posted. The projection decides on sign and status mapping. This keeps the phone as dumb as spend's model output, and a mapping bug can then be fixed with spend rebuild.
    Capture generously. rebuild can only re-derive from what was captured, so include every FinanceKit field you might plausibly want later. The data is encrypted and yours. The minimization goal is about where the data goes, not how much of it you store.
    Serialize money as decimal strings, never as JSON floats.

Keep FinanceKit types out of the network layer. The pipeline is FinanceKit model → local SwiftData model → event DTO → JSON.
Phase 4: Add the ingest endpoint to spend

Put the endpoint next to the existing receipt-upload route. It should be tailnet-only, like the rest of spend. The path /ingest/financekit below is a placeholder, so match whatever routing spend already uses.

The endpoint does the following:

    Checks the bearer token.
    Validates the shape of each event: it parses, has a known type, has a supported schema_version, and has the required fields present.
    Encrypts each event to the age public key and writes it as its own file: write to a temp file, fsync, then rename. Use a random ULID or UUID as the filename. Never put merchant names, amounts or FinanceKit IDs in filenames.
    Returns {"stored": n} only after every file in the batch is durable.

Some things it deliberately does not do: decrypt, look anything up, deduplicate, or categorize. Because of this it works identically whether the store is locked or unlocked.

Two kinds of bad input get different handling:

    Structurally invalid input returns 422. The phone keeps those events queued, since this is a bug in your own app. Fix it and they'll resend.
    Structurally valid but semantically odd input, such as an unknown status value or an unfamiliar transaction type, is stored as-is. The projection surfaces it as a failed attempt on the next rebuild. This matches the "failures show up as gaps" rule.

Duplicates are expected and harmless. The phone will resend after lost responses, and the projection absorbs them.
Phase 5: Write the FinanceKit projection

Add this to spend rebuild:

    Collapse by ID. Group transaction.observed events by fk_transaction_id. The latest observed_at wins, with event order as the tiebreaker. A transaction.removed event tombstones the transaction. Earlier observations remain available as history.
    Map accounts. Map fk_account_id to a spend account. Do this in config, alongside the existing TOML files, rather than through automatic creation, so you choose what "Apple Card" is called and which account it belongs to.
    Derive sign and status. Compute the sign from the credit/debit indicator, and map FinanceKit statuses onto spend's own states.
    Categorize. Run the existing categories.toml and merchants.toml rules. The merchant category code (MCC) is a strong extra signal, so consider letting rules match on it.
    Handle Apple Card payments. A payment from checking shows up twice: as a credit on the Apple Card via FinanceKit, and as a debit on checking via SimpleFIN. flows.toml should pair these two as a transfer so the payment isn't counted as spending.
    Reconcile balances. Compare balance.observed events against the derived running total, and surface mismatches as a warning. Don't auto-correct them.
    Fail visibly. An event the projection can't interpret produces a failed attempt, no transaction, and an entry in the UI for manual handling.

Phase 6: Resolve overlap with other sources

    Receipts. A photographed receipt and a FinanceKit transaction for the same purchase should link together, with the receipt's line items attached to the FinanceKit transaction, rather than becoming two transactions. Reuse whatever matching spend already does for receipts against bank-feed rows.
    Apple Card CSV/OFX exports. If you've been dropping monthly exports into the import folder, pick one source of truth per account. FinanceKit rows have stable IDs and exports don't, so set a cutover date: exports before it, FinanceKit after it. Then stop dropping exports for those accounts.
    SimpleFIN. Check whether any Apple accounts also come through SimpleFIN. If they do, apply the same cutover.

Phase 7: Sync on the phone

    Outbox. Each SwiftData record tracks pending, syncing, synced or failed, plus retryCount, lastAttemptAt and lastError. A record is marked synced only after a 2xx response with stored.
    History tokens. Read changes, persist them to the outbox, and only then save the new token. Upload asynchronously after that. Never save the token before the changes are stored locally.
    Batches. Send 50–100 events per request. The server's answer is a single count, so each batch succeeds or fails as a whole, which is simpler than handling partial failures.
    Initial import. Pull all available history on first run.
    Background extension. Keep the work minimal: query changes, write them to the outbox, try one upload, and exit. No categorization or enrichment happens on the phone.
    Data protection. The extension may run while the phone is locked, so the shared SwiftData store in the app group needs completeUntilFirstUserAuthentication rather than complete.
    Pruning. Once events are confirmed synced, delete their payloads from the outbox after a few days, and keep only the ID and status. FinanceKit is still the source on the device, and spend holds the durable copy.
    Tailnet reachability. If Tailscale isn't connected when the extension wakes, the upload fails and the events stay queued until the next wake or the next time you open the app. Don't promise real-time delivery. Apple controls when Wallet receives the transaction and when your extension gets to run.

Phase 8: Auth and transport

    The endpoint is reachable only over the tailnet and is never exposed publicly.
    Generate the bearer token with openssl rand -hex 32. Store it in a Keychain access group shared by the app and the extension.
    A useful property of this design: the token only grants append. If it leaked, an attacker could add junk events, which would show up as oddities on rebuild. They could never read the ledger, because the ingest process can't read it either. Add a request size cap and a basic rate limit anyway.

Optional hardening: encrypt on the device. The phone could age-encrypt each event to spend's public key before sending it, so the ingest service only ever handles ciphertext. The trade-off is that the server can no longer validate shape, so malformed events would surface only at rebuild time. Before committing to this, check that a maintained Swift age implementation exists.
Phase 9: Observability without plaintext

    Ingest log. The ingest service keeps a plaintext log with no content in it: a timestamp, the event count per type, and the HTTP status. It never records merchants or amounts.
    spend doctor. Add a check such as "last FinanceKit event written N hours ago", based on the ingest log. Warn past a threshold, for example 48 hours. This check works while the store is locked.
    Phone diagnostics screen. Show FinanceKit authorization, background delivery status, whether the history token is present, the pending outbox count, the last HTTP status and the last successful upload.
    Logging rule, everywhere. Log counts and status codes only, like "Uploaded 37 events, 200". Never log merchant names or amounts.

Phase 10: Test failure modes deliberately
Situation 	Expected result
Airplane mode, then buy something 	Event queued; appears in spend after reconnect
Tailscale disconnected 	Queued; flushes on next wake or app open
spend store locked 	Ingest succeeds; transaction appears after unlock and rebuild
Response lost, batch resent 	Two identical events; one transaction after rebuild
Pending → posted, amount changes 	One transaction with the final values; history shows both observations
FinanceKit reports a deletion 	Transaction tombstoned on rebuild
Unknown status value 	Stored; rebuild shows a failed attempt, not a wrong number
Malformed payload (app bug) 	422; events stay queued on the phone
Invalid token 	401; upload stops; outbox preserved
Phone restarts mid-sync 	Outbox and history token consistent; nothing lost
Apple Card payment from checking 	Paired as a transfer, not counted as spending
Receipt + FinanceKit for the same purchase 	One transaction with line items
CSV export overlapping FinanceKit 	No double counting after the cutover date
spend rebuild run twice 	Identical derived state
What you give up, deliberately

    No real-time alerts from spend. Derivation happens only when the store is unlocked, which is the point of the design. If you later want purchase notifications, have the phone app post a local notification, since the phone already holds the plaintext.
    No webhooks. Pushing derived data out would undercut encryption at rest. The downstream interface is spend agent build instead, and it now includes Apple Card data with merchant category codes. That's directly useful for the "which card should I use" advice goal.

Commit sequence

    Fork, rename, and build unmodified on device.
    Remove Lunch Money UI, API, asset pairing, and the push server.
    Add the event DTOs and the SpendAPI client.
    Rewire SyncBroker to the outbox → SpendAPI path.
    spend: add the ingest endpoint and the ingest log.
    spend: add the FinanceKit projection, account mapping, and flows pairing.
    spend: add receipt linking and the CSV/SimpleFIN cutover.
    Verify background delivery on device.
    Add spend doctor checks and the phone diagnostics screen, then run the failure-mode table.

Success criterion

    I tap my Apple Card while the spend store is locked. Later, without exporting anything, I run spend unlock, and the purchase is in my ledger. It has been categorized by my own rules, linked to its receipt if I photographed one, and it existed only as ciphertext on my server in the meantime. Nobody but Apple was in the path.

