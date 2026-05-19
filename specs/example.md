# Rate Limit Status Endpoint

## Objective
Expose a /status endpoint on the sample app that reports application health and is subject to per-IP rate limiting to protect against abuse.

## User Story
As an operator, I want a public /status endpoint that returns liveness and uptime info, but is rate-limited per client IP so it cannot be used for abuse or denial-of-service.

## Business Rules
- The endpoint returns HTTP 200 with JSON body {"status": "ok", "uptime_seconds": <int>}.
- Each client IP is allowed at most 5 requests per 10-second window.
- Requests over the limit receive HTTP 429 with a Retry-After header.
- Rate limit state is kept in-memory; no external store required for the prototype.

## Acceptance Criteria
- AC-1: GET /status returns 200 and a JSON body with status="ok" and a non-negative integer uptime_seconds.
- AC-2: The 6th request from the same IP within 10 seconds returns HTTP 429.
- AC-3: HTTP 429 responses include a Retry-After header with a positive integer value.
- AC-4: Requests from different IPs are tracked independently and do not share quota.

## Non-functional Requirements
- Response time under 50ms at p95 for the happy path.
- No external dependencies beyond the existing sample-target requirements.

## Out of Scope
- Persistent rate-limit storage across restarts.
- Configurable per-route limits.
- Authentication.
