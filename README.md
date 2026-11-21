# Ethiopia Building and Rental Registry (v0.5)
Run: `pip install -r requirements.txt && cp .env.example .env && set -a && . ./.env && uvicorn app.main:app --port 8000`
Open http://localhost:8000. Sign up creates an owner. The admin from ADMIN_EMAIL creates organizations (woreda office, tax authority, ...) with their accounts and powers. API docs: /docs.
Flow: owner registers building, uploads a front photo, submits; officer approves (one BRN); owner adds units (m²) and contracts; overlapping contracts are blocked (lock plus a PostgreSQL exclusion constraint when DATABASE_URL is PostgreSQL); QR letters open /verify/<token>.
Tests: `pytest -q`
## Not yet built (needs decisions or credentials)
MFA, rate limiting, Fayda/eTrade/MOR/DARS integrations (none connected; Fayda is stored masked only and never shown verified), tax module, inspections, disputes, notifications, exports, Amharic translations, S3 storage, Flyway/Alembic migrations.

## v0.2 additions
MFA (TOTP) for any account (set `REQUIRE_MFA=1` to force it for officers, admins and tax officers), login rate limiting, in-app notifications, inspections with GPS, disputes (officer freeze keeps the unit blocked), tax rule versions plus rental-income declarations (estimates only; no rate is built in, the admin or tax officer enters it), manual eTrade/DARS/Fayda verification records, CSV exports (no ID numbers), `/admin/integrations` status.
Roles: owner, staff (a member of an organization) and admin. Before v0.4 there were also officer and tax; see v0.4. Most v0.2 endpoints are API-only (see /docs); the web screens cover notifications, exports, MFA and the tax report.
Still open: Amharic UI, S3 storage, migrations (delete registry.db when upgrading from v0.1), encrypted MFA secrets, PDF/Excel export, real integrations.

## v0.3
Screens added for inspections, disputes (freeze and resolve), income declarations, tax rules and manual verifications. Excel export. MFA secrets are encrypted at rest (keep SECRET_KEY or MFA_KEY fixed). English/Amharic toggle: the Amharic is a machine draft for key screens and needs review by a native speaker. Printing a letter to PDF uses the browser's Print dialog.
Still open: S3 storage, database migrations, real integrations.

## v0.3.1
Fuller Amharic: the toggle now also translates status words, roles, buttons, prompts, success and error messages (including server errors), notifications, the printed letters and the page title. User-entered data (names, notes, tenants) is never translated. It is still a machine draft: have a native speaker review it, especially legal wording. Text not in the dictionary stays in English; add entries to the `AM` object in `app/static/index.html`.

## v0.4: organizations and powers
Every party logs in with its own account and uses only its own powers. The administrator creates an **organization** (woreda office, kebele office, city or sub-city office, revenue authority, trade office (eTrade), documents authentication (DARS), national ID (Fayda) or other), gives it an **area** (a woreda, optionally a kebele; empty means every woreda), ticks the **powers** it holds and adds accounts inside it. Staff see only the tools their powers allow, and the server checks every action, not just the screen.

| Power | What it allows |
|---|---|
| `registry.read` | View buildings and contracts in its area (not drafts). Added automatically to any organization that can act on records |
| `building.review` | Approve (issues the BRN), reject or return a building registration |
| `contract.approve`, `contract.terminate` | Approve or terminate rental contracts |
| `dispute.freeze`, `dispute.resolve` | Freeze a contract by opening a dispute; resolve disputes |
| `inspection.record` | Record inspections |
| `verify.etrade`, `verify.dars`, `verify.fayda` | Record manual verifications of that kind only |
| `tax.rules`, `tax.reports` | Set tax rule versions; view rental-income declarations |
| `audit.view` | View the whole audit trail, all offices included, so grant it carefully |

- **Starting powers are suggestions.** `app/policy.py` lists what each type starts with (a woreda office starts with what the old officer role could do). The registry owner decides what each office is really mandated to do; change the powers any time from the administrator screen (Powers) or `PATCH /api/v1/admin/orgs/{id}`.
- **Area.** Woreda and kebele offices must have an area. Names are matched ignoring capitals and spaces at the ends. The owner's woreda box suggests the woredas that have an office, so spelling matches.
- **Who approved.** The approving organization is saved on the building and the contract, printed as "Issuing office" on the letter, shown on the public `/verify` page and written to the audit trail as `email (organization)`.
- **Waiting for you.** When an owner submits a building or a contract, the accounts that can approve it in that woreda get a notification. If no office exists for the woreda, the owner is told it will wait (`reviewers: 0` in the submit answer).
- **Switching off** an organization or one account takes effect at once, even for a signed-in session. The administrator cannot switch off their own account.
- The administrator account keeps its old powers (audit trail, tax rules and report, verifications) and manages organizations; it does not approve buildings or contracts.

API: `GET /org-meta`, `GET|POST /admin/orgs`, `PATCH /admin/orgs/{id}`, `POST /admin/orgs/{id}/members`, `POST /admin/users/{id}/active`, `POST /me/password`. `/me` and login now return `powers`, `org` and `has_mfa`.

**Upgrading from v0.3:** nothing to do by hand. On start the app adds the new columns (`users.org_id`, `buildings.issuer`, `rental_contracts.issuer`) and moves old `officer` accounts into "<woreda> woreda office" and old `tax` accounts into "Tax authority", with exactly the powers they had. `role: officer` and `role: tax` still work in `POST /admin/users` and do the same. From v0.1, delete registry.db as before.

Also fixed: the "Set up MFA" button called a function that did not exist (it now shows the key and an authenticator link, and still appears when a screen cannot load because MFA is required); accounts can change their own password; `/me` and login no longer return the encrypted MFA secret; photo categories are limited to front, side, entrance and ownership (the text was free and shown to officers); duplicate email in `POST /admin/users` answers 409 instead of 500.

Still open, needs a decision: a chain of approvals (for example kebele endorses, then woreda approves; today one office decides each step), different powers for different people inside one organization (all accounts of an organization share its powers), tenant accounts, password reset and invitations by email, an audit trail limited to each office's own records. The new Amharic text is a machine draft like the rest.

## v0.4.1: deployment notes
Photos and documents are now stored in the database (table `building_file_data`), so nothing important lives on the app's disk and hosts with an ephemeral filesystem no longer lose uploads. Files that an older version saved on disk are still served while they exist. The 8 MB per-file limit is unchanged; uploads count against the database's storage.
The app listens on `$PORT` when the host sets one (default 8000). `DATABASE_URL` may be the plain `postgresql://` URL that hosts show; the app switches it to the psycopg 3 driver.
Set `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` and `DATABASE_URL` as environment variables. Without a PostgreSQL `DATABASE_URL` the data sits in a SQLite file on the app's disk and is lost on every restart on ephemeral hosts.

## v0.5: building structure, shop photos, tenant documents
- **Sub-city** is now required when an owner registers a building (suggestions list the sub-cities of Addis Ababa; any text is accepted). It shows on the building card, the registration letter and the CSV export. Older buildings keep an empty sub-city.
- **Floors.** After saving the draft the owner adds each floor (label such as G, 1, 2; optional name; optional floor area in m²). The number of floors entered cannot exceed the floors declared, and **submitting for approval needs every floor entered**. When a floor area is given, the shops on it may not add up to more.
- **Shops under each floor** (shop number, name, m², use). Shops can be added while the building is a draft, being corrected, or approved (not while under review or rejected). Old units are attached to a floor automatically on first start.
- **Shop photos**: JPG or PNG, up to 10 per shop, 8 MB each. The owner can remove a wrong photo.
- **Tenants**: the rental contract now also holds phone, TIN, trade licence number and business activity. Contracts can only be added once the building is approved.
- **Tenant documents** per contract: ID/Fayda copy, trade licence, TIN certificate, lease agreement, authorization letter, other (JPG, PNG or PDF, up to 15 each, with optional document number and expiry date). The owner can remove a document only while the contract is still waiting for approval; the audit trail keeps each file's fingerprint.
- **Who can open tenant documents:** the building's owner and offices that hold the new power `tenant.docs`. No office has it by default: the administrator ticks it per organization (Powers). The administrator account itself cannot open them. Every time an office opens one, it is written to the audit trail. Other offices still see the floors, shops, photos and tenant details, and are told how many documents are on file.
- New endpoints: `POST /buildings/{id}/floors`, `PATCH|DELETE /floors/{id}`, `GET /buildings/{id}/structure`, `POST /units/{id}/photos`, `POST /contracts/{id}/files`, `DELETE /files/{id}`. `POST /buildings/{id}/units` takes `floor_id` (or the floor label) plus `shop_name` and `notes`.
- **Upgrading from v0.4:** nothing to do by hand; the new columns and the floor records are added on start. Existing buildings that are still drafts will be asked for their floors before they can be submitted.
- Amharic: the new labels are a machine draft like the rest; messages from the server stay in English.

## v0.5.1: rent payments with receipts
For every approved contract the building owner records each rent payment: month, amount paid, payment date, method (Telebirr, CBE Birr, bank transfer, cash, other), receipt or transaction number, and the **receipt file (required**, JPG, PNG or PDF). `POST /contracts/{id}/payments`, `DELETE /payments/{id}` (owner, within 48 hours; the audit trail keeps it). The structure screen shows the payments and the total paid per tenant. Receipts follow the same rule as tenant documents: owner and offices with `tenant.docs` only; other offices see the amounts and dates.
