# Settings popup tabs (2026-11-28)

Owner request: split the SETTINGS popup into tabs: Account, Broker, OpenAlgo, LLM / cloud API, Infrastructure, Profile.

Decisions from the owner:
1. Profile picture lives in the Profile tab only.
2. Password change and 2FA live in the Account tab. The forgot-password flow stays on the sign-in box.
3. LLM keys are saved into the existing .env file through the credential store (2FA code required).

Changes:
- dashboard/main_dashboard.html
  - SETTINGS popup now has six tabs. One 2FA code box serves all credential saves.
  - Account: change password, 2FA key and QR (moved from the top-right box).
  - Broker, OpenAlgo, LLM / cloud API, Screener login: one credential group each, auto-save on leave.
  - Infrastructure: status list, Full Auto, secrets (.env), scheduler, engine.
  - Profile: picture upload, sign out. Signed out: a Sign in button opens the top-right box.
  - The profile-picture button opens the Profile tab when signed in.
  - Credential fields are not rebuilt while the owner is typing in the popup.
- src/quant/core/credential_update.py
  - LLM group now also holds OLLAMA_URL and ANTHROPIC_API_KEY.

Tests: full suite 1137 passed, 1 skipped.
Not changed: API routes, trading path, live settings.
