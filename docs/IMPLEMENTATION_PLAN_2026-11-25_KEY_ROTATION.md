# Implementation plan: automatic dashboard key rotation (2026-11-25)

Flow stage: Dashboard, account sign-in
Status: IN PROGRESS on branch feature/auto-key-rotation-2026-11-25. NOT on master.
Live stays off. Nothing here places or changes orders.

## 1. Why
The owner asked that the dashboard API token and the dashboard session secret
update automatically, not by hand.

Findings from the code on master c818caa:

| Key | Where it is used today | Auto-update possible? |
|---|---|---|
| `DASHBOARD_SESSION_SECRET` (signs login cookies) | `dashboard/app.py`. Env value wins; otherwise `data/session_secret.key` is created once and reused forever. | Yes. The file can be rotated safely. |
| `DASHBOARD_API_TOKEN` (`X-Dashboard-Token`) | **No code reads it.** Only `.env.example` and old docs mention it. The token route was removed on 2026-11-12: every write now needs the owner sign-in. | No effect to rotate. See section 4. |

## 2. Design (session secret)
* A new module `src/quant/core/key_rotation.py`:
  * the key file's modification time is the key's creation time (no format change,
    so the existing `load_or_create_secret` and its test keep working);
  * `rotate_if_due(path, max_age_s, now)` writes a new random key atomically
    (temp file, owner-only 0600, `os.replace`) only when the key is older than the
    maximum age;
  * no rotation runs while the env var `DASHBOARD_SESSION_SECRET` is set (an env
    pin is the owner's explicit choice, and the code must not hide it).
* `DASHBOARD_KEY_MAX_AGE_DAYS` (default 30). `0` turns rotation off.
* `dashboard/app.py`:
  * the signing key lives in a holder, so a rotation takes effect at once;
  * it is checked once at start and then every hour in a daemon thread;
  * each rotation writes one audit row (`session_key_rotated`, no key value);
  * `/api/infrastructure` gets a "Login key rotation" row.

Effect for the owner: after a rotation, every signed-in browser is logged out
once and must sign in again. This is the point of rotating.

## 3. Safety rules
* A rotation never writes the new key anywhere except the owner-only key file.
* No key value is ever logged, returned by an API, or put in the audit log.
* A failed rotation keeps the old key (fail closed to the working state, not to
  an empty key).
* Account login still refuses with no key (unchanged).

## 4. Open decision for the owner: DASHBOARD_API_TOKEN
Because no route checks `X-Dashboard-Token` any more, rotating that token would
change nothing. Re-adding a token login to the write routes would reopen a
write path that was closed on 2026-11-12. This plan does NOT re-add it. Owner
to choose:
1. Leave the token retired (recommended), and remove it from `.env.example`
   in a later change; or
2. Re-add token auth on purpose, with rotation, in a separate plan.

## 5. Tests
* `tests/unit/test_key_rotation.py`: not due = no change; due = new value, mode
  0600, old value gone; env pin = never rotated; max age 0 = off; bad max-age
  value = default; a failed write keeps the old key.
* Existing dashboard tests must still pass (login key creation, sessions).

## 6. Done criteria
* Full unit suite passes, same failures as master only (none expected).
* Branch pushed to origin; master untouched; no live setting changed.
