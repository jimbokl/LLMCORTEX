# VetCore Public URL Pack

Date: 2026-06-21

This directory is a static URL pack for App Store Connect:

- `app-store-page.json` -> marketing URL fallback
- `privacy-policy.json` -> privacy policy URL fallback
- `support-page.json` -> support URL fallback
- `index.html` -> marketing HTML page for hosts that serve HTML normally
- `privacy.html` -> privacy HTML page for hosts that serve HTML normally
- `support.html` -> support HTML page for hosts that serve HTML normally
- `assets/vetcore-icon.png` -> selected VetCore app icon
- `app-store-urls.json` -> App Store URL manifest

The current live host serves HTML and text pages with HTTP 403 while serving
`.json`, `.css` and `.png` publicly. Use the JSON URLs in App Store Connect
until the host configuration is fixed:

```bash
PETVET_MARKETING_URL="https://jimbokl.github.io/LLMCORTEX/vetcore/app-store-page.json" \
PETVET_PRIVACY_POLICY_URL="https://jimbokl.github.io/LLMCORTEX/vetcore/privacy-policy.json" \
PETVET_SUPPORT_URL="https://jimbokl.github.io/LLMCORTEX/vetcore/support-page.json" \
PETVET_SUPPORT_EMAIL="mmotorin@gmail.com" \
PETVET_LEGAL_ENTITY="Dmitriy Motorin" \
scripts/petvet_store_live_url_check.sh --require-live
```

For final submission, also provide App Store Connect id, production archive
path, real-device QA marker and human approval, then run:

```bash
scripts/petvet_app_store_submission_preflight.sh --require-external
```
