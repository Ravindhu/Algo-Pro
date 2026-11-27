# Implementation plan: save the OpenAlgo API key after login (2026-11-27)

Flow stage: Dashboard sign-in (credential upkeep; no order path)
Status: DONE (tests green)

## Goal (owner decision: "read the existing key")
After a successful sign-in (OpenAlgo password or 2FA, then broker TOTP, then the session check),
read the CURRENT OpenAlgo API key and save it as OPENALGO_API_KEY in _restored/.env.

## Rules
- Read only: GET /apikey returns the current key. Nothing is generated, so no key is revoked.
- Write only when _restored/.env already exists (never created here), and only when the value differs.
- The key value is never logged, returned in a result, or written to the audit log. Only fixed
  outcome words are recorded ("updated", "unchanged", "no key yet", ...).
- The step is NON-FATAL: a failure here does not fail the broker login.
- If OpenAlgo has no key yet, nothing is generated; the owner is told to create one at /apikey.

## Steps
1. [ ] src/quant/core/openalgo_apikey.py: fetch_key(get), sync_env(key, env_path), read_env_value.
2. [ ] broker_auth.BrokerAuth.sync_api_key(result), called from run() after a successful verify.
3. [ ] Tests: fetch outcomes, sync outcomes (missing file, same, changed, other lines kept),
       no key value in any step or result, login result still ok when the key step fails.
4. [ ] Full suite, secret scan, commit, push branch, fast-forward master.

## Not verified here
The live GET /apikey response from a running OpenAlgo (sandbox cannot reach it). First live
sign-in should be checked in the audit steps: step "openalgo_apikey".
