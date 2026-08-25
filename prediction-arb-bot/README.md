# Kalshi–Polymarket US scanner

This is a **read-only, alert-only** service. It has no order, position, wallet,
or fund-movement endpoints. Once per process, before public-market scanning can
become ready, it performs one signed read-only retail access probe and checks
only the HTTP status. The response body is never read, stored, logged, or
exposed.

It fails closed by default:

- Official Polymarket US documentation (checked August 24, 2026) identifies
  `https://gateway.polymarket.us` as the unauthenticated public market-data host
  and `https://api.polymarket.us` as the authenticated retail host.
- Polymarket US documents that identity verification must be approved before a
  user can access the Developer Portal and create a retail API key. The current
  key is verified at process startup with a signed, read-only
  `GET /v1/account/balances` request. Only HTTP 200 establishes readiness. The
  response body is deliberately not read, printed, stored, or exposed. A failed
  request stands down before the public catalog is queried.
- Successful access is cached only in process memory, so a new process must
  prove access to the official retail host again. No local assertion, evidence
  file, or credential-generated signature by itself can establish readiness.
  Health output exposes only `authorization.ready` and
  `authorization.status`; it never includes the balance response, account
  details, credentials, positions, or funds.
- The institutional `/v1/whoami` and `/v1/accounts` endpoints are not used:
  retail credentials do not support those routes.
- `POLYMARKET_US_PUBLIC_API_BASE_URL` defaults to the official public gateway,
  and other hosts are rejected.
- Both venue fee caps must be explicitly set with
  `PREDICTION_ARB_KALSHI_MAX_TAKER_FEE_CENTS` and
  `PREDICTION_ARB_POLYMARKET_MAX_TAKER_FEE_CENTS`, then acknowledged with
  `PREDICTION_ARB_FEES_VERIFIED=true`. Values must be positive; missing,
  malformed, or zero values stand down.
- Every reviewed pair must also pin positive venue-specific fee caps, the exact
  official fee-evidence URLs, and a review timestamp no older than 30 days.
  Opportunity math uses the higher of the global and pair-specific caps.
- `reviewed_matches.json` must contain a human-reviewed equivalence record for
  every pair. The scanner compares exact market identifiers, settlement-rule
  fingerprints, resolution sources, and event cutoff time. It never pairs titles.
- Every registry entry must state `kalshi_yes_means` and
  `polymarket_yes_means` with exactly equivalent outcome semantics; free-text
  notes alone cannot establish complementary outcome mapping.
- Both venues must publish the same event start time, and it must be between
  `PREDICTION_ARB_MIN_EVENT_HORIZON_HOURS=1` and
  `PREDICTION_ARB_MAX_EVENT_HORIZON_HOURS=72` from observation. Long-dated
  political, season, and championship futures are rejected.
  Adapter fallbacks from expiration, close, or settlement deadlines are not
  accepted as event-start evidence.
- Quotes must be fresh, have depth on both legs, and remain net-positive after
  configured fees and slippage. Freshness comes only from an upstream
  quote/update timestamp, never from local receipt time.
- Polymarket US catalog rows are mapped from `marketSides`, including the
  published `feeCoefficient`. For a reviewed market, executable YES/NO asks and
  depth come from the official public `GET /v1/markets/{slug}/book` response,
  and freshness comes from its provider `transactTime`.
- Kalshi catalog rows are enriched from the official event endpoint so exact
  `rules_primary` text and event `settlement_sources` are available. Fixed-point
  YES ask depth comes from `yes_ask_size_fp`; NO ask depth uses the equivalent
  best-YES-bid quantity in `yes_bid_size_fp`. Missing rules, settlement sources,
  prices, depths, timestamps, or required Polymarket fee data rejects the row.
- Catalog health remains lightweight while `reviewed_matches.json` is empty:
  `market_count` is the number of live rows returned by the broad catalog
  request and
  `usable_market_count` is the number of requested, fully enriched rows.
  Rules and order-book enrichment is requested only for exact IDs already in
  the human-reviewed registry. Reviewed IDs not present in the broad catalog
  page are fetched from the venue's official exact-market endpoint, so catalog
  pagination cannot hide an approved pair. A nonzero catalog count never
  enables a pair.

`GET /health` reports source readiness and a clear `stand_down` reason.
`GET /api/opportunities` returns dashboard-safe, non-executable opportunities.


## Official fee evidence

The scanner uses conservative whole-cent fee caps per contract because its
opportunities are computed per contract:

- Kalshi's official fee schedule, checked August 23, 2026, lists standard
  prediction-market taker fees from $0.07 to $1.75 per 100 contracts and warns
  that some series have different fees. A reviewed pair must use a cap at least
  as high as that pair's current official series fee; `2` cents covers the
  current standard maximum after rounding up, but must not be assumed for a
  non-standard series without reviewing the series row.
- Polymarket US's official schedule is effective July 1, 2026 and gives taker
  fee `0.06 × contracts × p × (1 - p)`, with a maximum of $1.50 per 100
  contracts at $0.50. A conservative whole-contract cap is therefore `2` cents.
  Volume rebates and maker rebates are intentionally ignored.

Official sources:

- https://docs.polymarket.us/api-reference/introduction
- https://docs.polymarket.us/api-reference/authentication
- https://docs.polymarket.us/api-reference/account/get-account-balances
- https://docs.polymarket.us/api-reference/markets/get-markets
- https://docs.polymarket.us/api-reference/markets/get-market-book
- https://docs.polymarket.us/partners/onboarding/accounts
- https://docs.polymarket.us/fees
- https://docs.kalshi.com/api-reference/events/get-event
- https://docs.kalshi.com/api-reference/market/get-market-orderbook
- https://kalshi.com/fee-schedule
- https://help.kalshi.com/en/articles/13823805-fees

`reviewed_matches.json` remains empty until a complete, manually reviewed live
pair is available. The public gateway request is explicitly limited to
`active=true&closed=false`; adding a fabricated or title-only pair would violate
the registry's safety contract.

## Short-duration pair review

On August 24, 2026, the full-match **DYNASTY vs. Team Synapse Dota 2**
candidate scheduled for August 25 at 19:00 UTC was manually reviewed:

- Kalshi market:
  `https://api.elections.kalshi.com/trade-api/v2/markets/KXDOTA2GAME-26AUG251500SYNDYN-DYN`
- Polymarket US market:
  `https://gateway.polymarket.us/v1/markets?slug=aec-dota2-dyn-tsea-2026-08-25&active=true&closed=false`
- Both YES outcomes meant DYNASTY wins the full match, and both referred to the
  same scheduled event.
- Kalshi listed BO3, Dota 2, DLTV, and Gamers World as settlement sources;
  Polymarket US named Valve. The resolution authority therefore was not exact.
- Kalshi resolves to fair-market price if the match does not start within 48
  hours. Polymarket US can wait up to two weeks before using fair-market price.
  A match starting after 48 hours but within two weeks could settle differently.
- Kalshi's series used its standard quadratic fee with multiplier 1; Polymarket
  US reported fee coefficient 0.06. The configured 2-cent conservative cap per
  contract covered each published schedule at review time.
- Both books had executable depth, but rule equivalence is an absolute gate.

The pair was rejected and was not added to the registry. This is intentional:
no real opportunity may appear until a short-duration pair has exact settlement,
authority, timing, cancellation, and YES-outcome equivalence.

## Telegram alerts

The scanner can send a one-way informational message using the existing Crypto
Bot token. It never polls Telegram, manages subscribers, or enables orders.
Set the destination as the secret `PREDICTION_ARB_TELEGRAM_CHAT_ID`, then set
the non-secret environment variable `PREDICTION_ARB_TELEGRAM_DELIVERY_ENABLED=true`.
Only newly deduplicated, validated opportunities are sent. A delivery failure
does not retry the alert, to avoid duplicate time-sensitive notices.

When preparing a reviewed registry entry, generate a rule fingerprint with:

```sh
python -c "from scanner import rules_fingerprint; print(rules_fingerprint('exact published rule text'))"
```
