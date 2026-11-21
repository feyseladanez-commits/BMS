# Ethiopia Building and Rental Registry (v0.4)
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
