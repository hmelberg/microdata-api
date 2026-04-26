# Setup notes for the Anvil app

Operations that have to be done outside the code (in the Anvil IDE / Azure portal). Keep this file updated as new external dependencies appear.

## Anvil Secrets

Add these in **Anvil IDE → Settings → Secrets**. Names must match exactly.

| Secret | Required from | Format / example | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Phase -1 (existing) | `sk-ant-...` | Anthropic API for AI calls. |
| `API_KEY_ALIASES` | Phase -1 (existing) | `hans,evalbot` (comma-separated) | List of legacy X-API-Key aliases. |
| `API_KEY_<alias>` | Phase -1 (existing) | random urlsafe | One per alias listed above. |
| `BOOTSTRAP_ADMIN_EMAILS` | **Phase 0** | `hans.melberg@gmail.com,hans.olav.melberg@fhi.no` | First-login auto-promote to `is_admin=true`. Comma-separated, no spaces. |
| `AZURE_CLIENT_ID` | **Phase 1** | UUID, e.g. `a1b2c3d4-...` | Application (client) ID from Azure AD app registration. |
| `AZURE_TENANT_ID` | **Phase 1** | `common` (multi-tenant) or specific tenant UUID | Tells PyJWT which issuer to accept. Use `common` for multi-tenant. |
| `MAGIC_LINK_SIGNING_KEY` | **Phase 1** | random 32+ byte string | Optional — used if magic-link tokens become signed JWTs instead of opaque DB rows. |

## Azure AD app registration (Phase 1)

Register one multi-tenant application:

1. Azure Portal → Microsoft Entra ID → App registrations → **New registration**.
2. Name: `Microdata Script Runner`.
3. Supported account types: **Accounts in any organizational directory (multi-tenant)**.
4. Redirect URI: SPA, `https://micro.fhi.dev` (and `http://localhost:8080` for local dev if needed).
5. After creation, copy **Application (client) ID** → put in Anvil secret `AZURE_CLIENT_ID`.
6. Anvil secret `AZURE_TENANT_ID` = `common` for multi-tenant, OR specific tenant GUID for single-tenant.
7. **Authentication** blade → enable **ID tokens (used for implicit and hybrid flows)**.
8. **API permissions** → ensure `User.Read` (delegated, Microsoft Graph) is listed. Default permission is fine; admins of consenting tenants will be prompted on first login.
9. No client secret is needed (SPA + PKCE flow handles this).

## One-shot tasks after first deploy

After the schema has been picked up by Anvil (commit + sync from GitHub), open the Anvil server console and run:

```python
anvil.server.call("seed_phase0")
```

This creates default `limits_config` rows and an `@fhi.no` whitelist entry. Idempotent — safe to re-run.

## GDPR / personvern

`m2py/personvern.html` is a draft for review. Steps before going live:

1. Send to FHI personvernombud (`personvernombud@fhi.no`) for review.
2. Verify Anvil's data-region is EU (Anvil IDE → Settings → App region).
3. Confirm Anthropic Standard Contractual Clauses cover the legal basis for transferring AI prompts to the US.
4. Replace "Sist oppdatert" date when published.

## Service-tokens (legacy X-API-Key)

The X-API-Key path remains active for automation and the eval harness. Add new aliases by:

1. Generate a strong random key.
2. Anvil secret `API_KEY_<newalias>` = the key value.
3. Append `<newalias>` to `API_KEY_ALIASES`.

Service-tokens bypass user limits but are still subject to the existing 30 calls/min rate-limit.
