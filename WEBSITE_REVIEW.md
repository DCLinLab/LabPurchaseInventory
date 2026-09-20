# LabPurchaseBot personal project website draft

The site describes a personal, non-commercial project used by its maintainer and a small group of known lab colleagues, with no public signup, subscription, or paid offering. Its plain, single-column layout is intentionally closer to a project note than a product landing page.

This change adds only the static informational website in `docs/`. It does not enable GitHub Pages, change OAuth settings, or change the bot.

## Review
- Homepage: `docs/index.html`
- Privacy: `docs/privacy.html`
- Terms: `docs/terms.html`

All pages are marked as review drafts and carry `noindex`. They use local CSS, no JavaScript, no external fonts, and no analytics.

The operator/contact used is the Lin Lab project operator, linjhumse@gmail.com. No university endorsement or separate legal entity is claimed.

Before adopting these policies:
1. Confirm the operator/contact and the proposed notification and manual deletion commitments.
2. Review the OpenAI account's actual data controls, retention, and eligibility for processing Google API data. The draft discloses this processing and does not claim that provider training or retention is disabled. Do not add a blanket Limited Use compliance or no-training assertion without validating the account configuration.
3. Ensure connected account holders understand that relevant email/attachment content and spreadsheet records go to OpenAI and that results are visible in designated Slack channels and shared sheets. A website alone does not establish this consent.
4. Confirm workstation permissions and data protection. Google tokens use Windows DPAPI; local photo/email caches and other configuration are not all encrypted by the application. There is no automatic deletion schedule.
5. Set an effective date and remove the draft banners, draft labels, and noindex metadata when the operator approves publication.

## Proposed GitHub Pages setup (not enabled by this draft)
After review and merge, use repository Settings > Pages > Deploy from a branch > main > /docs. Serving only /docs keeps the website artifact limited to the public informational files. The existing repository itself is public.

Expected URLs, not yet live or verified:
- Homepage: https://dclinlab.github.io/LabPurchaseInventory/
- Privacy: https://dclinlab.github.io/LabPurchaseInventory/privacy.html
- Terms: https://dclinlab.github.io/LabPurchaseInventory/terms.html

Check the published URLs before entering them into Google Cloud Branding. The expected host is dclinlab.github.io; use the domain Google accepts in Authorized domains, without a scheme or path. Ownership verification, if requested, is a separate step. Do not register github.io as though the lab owns that entire domain.

Publishing these informational pages does not change the Google OAuth audience or publishing state. Do that separately after URLs work. Personal-use verification exceptions do not waive applicable data-use requirements.

## Reference material
- Google branding and homepage/privacy requirements: https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification
- Google API Services User Data Policy: https://developers.google.com/terms/api-services-user-data-policy
- GitHub Pages: https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages

Content reflects the bot source inspected September 20, 2026, including the current local query changes. Recheck if behavior changes before publication.

