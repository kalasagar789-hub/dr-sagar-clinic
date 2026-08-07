# Appointment API contract

Base URL: `/api/v1`. All write operations require an idempotency key and produce an `appointment_logs` audit event. JWT claims determine role access.

| Method | Endpoint | Purpose | Roles |
|---|---|---|---|
| GET | `/appointments?date=&providerId=&status=&mode=&patient=` | Filterable queue/list | reception, provider, admin |
| POST | `/appointments` | Create appointment; validates schedule/slot/non-overlap | reception, patient, provider |
| GET/PATCH | `/appointments/:id` | View or reschedule/edit appointment | scoped roles |
| POST | `/appointments/:id/check-in` | Atomically assign token and queue position | reception |
| POST | `/appointments/:id/vitals` | Record validated clinical vitals and calculated BMI | reception |
| POST | `/appointments/:id/status` | Allowed state transition | reception, provider |
| POST | `/appointments/:id/cancel` | Cancel with reason and reminder cancellation | reception, patient |
| POST | `/appointments/:id/reschedule` | Revalidate and move appointment | reception, patient |
| GET | `/providers/:id/slots?date=` | Available/booked/blocked/lunch slots | authenticated |
| PUT | `/providers/:id/schedule` | Set provider working schedule | provider, admin |
| POST | `/providers/:id/leaves` | Add leave/holiday | provider, admin |
| GET | `/queues/today` | Current/next tokens and ETA | authenticated |
| GET | `/teleconsultations/:appointmentId` | Meeting state and join details | scoped roles |
| POST | `/followups` | Create follow-up recommendation/booking | provider |
| GET | `/reports/appointments?groupBy=` | Appointment/revenue/queue reports | admin |

WebSocket channels: `queue:{clinicId}:{providerId}`, `appointment:{id}`. Publish status, token, and slot events after a successful database transaction. Validate mobile as E.164, time interval against provider schedule/leave, duration against appointment type, and all conflicting slots before booking.
