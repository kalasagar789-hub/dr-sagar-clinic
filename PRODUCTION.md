# Production go-live checklist

This application must not be exposed to patients or staff until every checkbox is completed by the clinic owner and technical administrator.

## Required deployment settings

- [ ] Use a managed PostgreSQL database. Production mode rejects SQLite.
- [ ] Set `CLINIC_ENV=production`, `SECRET_KEY`, `DATABASE_URL`, `PUBLIC_BASE_URL` and `SESSION_COOKIE_SECURE=true` as protected environment variables.
- [ ] Run the WSGI app with Waitress behind an HTTPS reverse proxy. Do not run `py app.py` in production.
- [ ] Use a real public HTTPS domain; laboratory QR verification links require it.
- [ ] Keep `CLINIC_AUTO_CREATE_SCHEMA=false` and `CLINIC_BOOTSTRAP_REFERENCE_DATA=false`.
- [ ] Apply a reviewed database migration before every release. Do not use `db.create_all()` to upgrade a production database.

## Authentication and privacy

- [ ] Configure an approved SMS/OTP provider with expiry, rate limiting, delivery audit and patient consent before enabling patient sign-in.
- [ ] Remove all local demonstration accounts and force every staff member to create a strong unique password.
- [ ] Define password reset, staff off-boarding, admin approval and emergency-access procedures.
- [ ] Obtain legal review for consent, retention, privacy notice, data-export and breach-response processes applicable to the clinic location.

## Clinical governance

- [ ] A registered doctor reviews every medicine, combination, dosage template and clinical advice template.
- [ ] A qualified pathologist reviews laboratory test definitions, units, age/sex reference intervals, critical-value rules and report sign-off policy.
- [ ] Keep AI output as an editable draft; never allow it to diagnose, prescribe, sign reports or modify values automatically.

## Operations

- [ ] Configure encrypted daily backups, off-site retention and a documented restore test at least quarterly.
- [ ] Monitor `/health`, database connectivity, failed logins, background errors and available storage.
- [ ] Test the full workflow in staging: reception → consultation → lab → verification → pharmacy → billing → patient portal.
- [ ] Test access controls for every role and revoke a staff account to confirm immediate access removal.
- [ ] Configure WhatsApp Business/SMS only after recording patient communication consent and opt-out preference.

## Release gate

The clinic administrator, technical owner and clinical owner must sign off this checklist. A feature-complete UI is not a substitute for these safeguards.
