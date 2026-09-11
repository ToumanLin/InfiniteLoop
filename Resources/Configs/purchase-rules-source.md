# Purchase rule sources

`table/share/pay/PurchaseDailyDuration.tsv` is an explicit local-server
configuration, not a recovered retail table. On 2026-09-10 the maintainer
authorized the Construct 10x R&D Pack -10d (package 9) to last 10 days:
“同意，按 10 天配置”. Runtime resolves the duration by package ID and derives
the remaining days from persisted entitlement state and the server reset clock.
Unknown non-monthly daily packages still require a configured duration.

Companion monthly-pass grants bypass the direct renewal cap. This follows the
installed EN client's `Text.tab`, key `PurchaseMonthPlusDesc`: the beginner
combo remains purchasable with six active passes and extends their term.
The combo's own one-time purchase limit and mutually exclusive pass checks
still apply. Direct monthly renewals retain their existing cap.
