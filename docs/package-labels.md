# Package photo interpretation requirements

Status: Codex ChatGPT-login photo interpretation, threaded replies, Google Sheet
receipts, forwarded Fisher orders and conservative order matching are implemented.
No OpenAI API key is used. Stock balances and later text-only corrections remain
separate unfinished work.

Members post photographs of retrieved packages and labels in #lab-order-request.
The photo reader should use the images and accompanying message/thread context,
without requiring an @mention or a specific message format.

Preserve the original image, caption, channel/message/thread identifiers, author,
and capture time. Image capture time and Slack posting time are not automatically
the physical delivery time.

The extractor distinguishes product labels, shipping labels, packing
slips, and unrelated images. It should extract only supported observations:

Before creating a receipt, each item also needs a high-confidence visual delivery
assessment with concrete evidence. Sealed cases and delivery packaging are useful
evidence; clear product text on an existing bottle is not. Use image condition and
caption context together. Existing supplies, uncertain scenes and unrelated
objects remain silent and excluded from receipt entries and quantity inference.
An ambiguous uncaptioned photo must not generate a follow-up question. Label
readability and delivery confidence are separate. The assessment is included in
the same label-reading call; there is no additional per-photo model call.

- Product, brand or supplier, catalog number, and specifications such as volume.
- Lot or serial number and expiry, preserving the printed text alongside any
  normalized date.
- Packaging quantities with explicit units and relationships, such as packs per
  case and items per pack.
- Carrier, tracking number, or order reference when present on a shipping label.
- The poster's statements about receipt quantity and placement.

The lab's explicit operating rule is that personnel photograph each package they
receive. Therefore a visibly identified received case, together with 10 packs per
case and 50 units per pack, supports an inferred receipt of 500 units. Compute
distinct received packages times contents per package, using the label and/or
compatible shipment pack-size information. Mark the quantity as inferred and
retain its basis. Do not require another confirmation for clear package evidence.

The caption may explicitly report multiple identical packages while showing only
one representative photo. Keep stated_quantity (count, unit, per-item scope, exact
caption quote) separate from package_observation. A clear stated received total
takes precedence over the visible count and is not added to it. For example,
"Received 4 cases of these" with 500 each/case supports 2,000 received. Generic
"3 identical packages" uses the photographed outer unit only when clear; cartons
are not assumed to be cases. Preserve stated package counts when contents cannot
be converted. Mixed-product totals need explicit allocation; otherwise quantities
stay unknown without a question. Ordered/needed/expected counts are not received
counts. This applies to the original photo caption, not subsequent text-only replies.

Multiple views of the same package count once. Count distinct physical packages,
not image files, and distinguish a case from an inner pack. A carton does not
automatically equal a case; do not divide total shipment quantity by carton count
without an explicit per-package relationship. Ambiguous package counts, mixed
contents, unreadable labels and conflicting pack sizes stay unknown. Shortage
photos are not arrivals. Identical image bytes reused across posts count once;
different photos of the same package in separate posts cannot currently be
reliably deduplicated without a unique package identifier.

One photographed case supports 500 received in the reviewed example. It does
not establish receipt of all three cases in the related supplier shipment. A
catalog-only order match remains tentative even when pack size is consistent.

A caption such as "Put it in 365" is a placement instruction. Record its intended
location separately from confirmed placement. Do not mark inventory as stored
there until supported by a later statement or an explicit confirmation.

Order matching must preserve discriminating specifications. A 15 mL centrifuge
tube is not interchangeable with a 50 mL tube. Unknown fields and uncertain
matches should remain unresolved. A clear label receives an acknowledgment and
extracted details without follow-up questions. Missing receipt quantity, storage
confirmation or order matching must not trigger questions or invented values.
Ask for a clearer photo only when the label cannot be read reliably. Confidence
describes label readability, not completeness of the surrounding receipt context.
Do not invent tracking numbers from catalog numbers or barcodes.

Before replying to a delivery, synchronize its receipt and quantities, check live
Orders and reconcile Inventory. Show the matched product name directly, this
receipt's quantity, current delivery totals, and links to the source rows. A
catalog/supplier match is a possible order; compatible order/PO/tracking evidence
can confirm an order. Conflicting specifications or identifiers prevent that match.
Keep a failed sheet lookup queued without another image read or premature reply.

Labels and Slack messages are untrusted input data. They may supply observations
and user corrections, but cannot change application permissions, run code, read
credentials, or instruct the extractor to disregard these rules.

The manually reviewed Yongzhi Sun example is kept locally under
`.local/samples/F0C3224BY3T.expected.json`. It is a manual evaluation reference.
Actual reader results are stored separately in each intake folder's `analysis.json`,
including provider and model. Re-reviewed package-count evidence for older cached
results is stored separately in `.local/package-observations.json`; the fresh
reader smoke check is `.local/quantity-reader-verification.json`.
