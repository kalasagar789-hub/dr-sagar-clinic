# Production release checklist — Dr. Sagar's Clinic

This application is suitable for continued development and controlled testing. Do **not** place real patient data in production until every item below has been completed, reviewed, and signed off by the clinic owner and technical lead.

## Before deploying

- Set `CLINIC_ENV=production`, a unique random `SECRET_KEY`, and `SESSION_COOKIE_SECURE=true`.
- Use managed PostgreSQL with encrypted storage. Do not use `clinic.db` / SQLite for production.
- Run schema setup and reference-data bootstrap in a controlled release step; keep both automatic bootstrap flags disabled during normal production runs.
- Install dependencies with `py -m pip install -r requirements.txt`.
- Run the service through Waitress, for example: `waitress-serve --listen=127.0.0.1:8080 wsgi:app`.
- Put Waitress behind a maintained HTTPS reverse proxy. Force HTTPS and configure the real public domain.
- Restrict database credentials and backups to authorized administrators. Rotate credentials regularly.

## Safety, privacy, and workflow readiness

- Replace demo patient OTP with a verified SMS/WhatsApp OTP provider, expiry, retry limits, and audit records.
- Add full CSRF protection to every state-changing HTML form and API endpoint before public internet exposure.
- Implement password reset, MFA for privileged roles, login throttling, account lockout policy, and an immutable user-access audit trail.
- Complete role-permission testing: reception, doctor, dietician, lab staff, pharmacy staff, admin, and patient.
- Validate all lab reference ranges and units with the pathologist. Only verified results should be released to doctors/patients.
- Obtain legal/privacy review for local health-data requirements, consent, retention, breach response, and data-processing agreements.
- Confirm clinical sign-off for prescription, diet-plan, lab-report and billing templates.

## Operations

- Configure encrypted, automated daily database backups plus a tested restore procedure.
- Set monitoring and alerts for uptime, failed logins, database storage, background jobs, and backup failures.
- Keep application, Python, database, and server security patches current.
- Test print/PDF output, low-stock alerts, financial totals, and workflow handoffs using a staging database.
- Create an incident runbook and train staff before launch.

## Go-live approval

Record the release version, database migration, backup restore test date, security review date, and clinic owner approval. A production release should be approved only after the open items above are resolved.
