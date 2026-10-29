# Public career feed relay

A small, dependency-free Python job that reads the official Wayfair and Microsoft career feeds and sends snapshots to owner-configured HTTPS ingestion endpoints. It supports a separate hosted career monitor when a career feed is unavailable from that monitor's hosting network.

Only public job data is handled. Eligibility filters and notification delivery remain in the receiving application. This directory contains no applicant details, phone notification topics, admin passwords or Cloudflare account credentials.

The prepared GitHub workflow runs once every five minutes with no continuous background loop. GitHub scheduling can delay or drop runs, so the receiving application may also start runs through `workflow_dispatch` on its own five-minute clock; a company checked moments earlier is skipped rather than fetched twice. Rate/access restrictions stop a company's current run; its cooldown is recorded in the receiver and respected by later runs. `Retry-After` is honored when sent; otherwise a rate limit (HTTP 429) is retried on the next scheduled run and an access refusal (HTTP 403) waits 15 minutes. No CAPTCHA, browser impersonation, authentication bypass or proxies are used. Wayfair uses overlapping keyword snapshots. Microsoft uses recent software pages, a rotating technical query and one historical page per run; full historical coverage is not claimed.

## Deployment

Publish this directory's `poll.py` and README in a repository, and place `workflow.yml` at `.github/workflows/relay.yml`. **A public repository with a standard GitHub-hosted runner is free independently of a private repository's compute allowance.** Public code is visible to others. Confirm that visibility choice before publishing; keep the receiver and application secrets private. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

Set these repository **secrets**:

- `WAYFAIR_INGEST_URL`: your Wayfair receiver's HTTPS `/api/feed-relay` endpoint.
- `MICROSOFT_INGEST_URL`: your Microsoft receiver's HTTPS `/api/feed-relay` endpoint.
- `MICROSOFT_LOCATION`: optional location setting; kept out of the public code.
- `CAREER_RELAY_KEY`: a dedicated key granting only feed ingestion and source health reporting; never use an administrator password or Cloudflare API credential.

Trigger the workflow manually once and check the receiver's completed-scan timestamp. Job data is sent in two-record chunks to respect a small receiver CPU budget. Microsoft descriptions are requested from a durable private queue, with at most three description requests per run. The private receiver chooses that queue using its own rules; queued descriptions can add detection time. A failed run does not mark an incomplete snapshot successful. Repeating a chunk is safe. Received publication dates are retained; discovery does not mean the role was newly published.

For a read-only local probe, set `RELAY_DRY_RUN=true` and run `python poll.py`. Nothing is relayed in dry-run mode.
