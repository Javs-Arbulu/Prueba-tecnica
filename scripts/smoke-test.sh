#!/usr/bin/env bash
# End-to-end smoke test against a running stack.
#
#   docker compose up -d
#   ./scripts/smoke-test.sh
#
# Walks the story a reviewer cares about — public intake, idempotency, login,
# the state machine, optimistic concurrency, the audit trail, what a customer
# may see, permissions — asserting the status code of every step and exiting
# non-zero on the first surprise.
set -euo pipefail

API="${API:-http://localhost:8000/api/v1}"
AGENT_EMAIL="${AGENT_EMAIL:-ana.reyes@support.local}"
OTHER_AGENT_EMAIL="${OTHER_AGENT_EMAIL:-luis.ferrer@support.local}"
PASSWORD="${DEMO_PASSWORD:-demo12345}"

# Unique per run: an idempotency key is honoured for 24h, so a fixed one would
# make the second run replay the first instead of creating anything.
RUN_KEY="smoke-$(date +%s)-$$"

pick() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
expect() {
  local want="$1" got="$2" what="$3"
  if [ "$want" != "$got" ]; then
    echo "   FAIL: $what -> HTTP $got (expected $want)" >&2
    exit 1
  fi
  echo "   ok   $what -> HTTP $got"
}

TICKET_BODY='{
  "customer": {"name": "Elena Duarte", "email": "elena.duarte@northwind.example"},
  "subject": "Cannot log in after the password reset",
  "description": "The reset link says it has expired and I have tried three times from two browsers.",
  "reported_priority": "HIGH"
}'

echo "== 1. Public intake, no authentication =="
RESPONSE=$(curl -s -w '\n%{http_code}' -X POST "$API/public/tickets/" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $RUN_KEY" -d "$TICKET_BODY")
expect 201 "$(echo "$RESPONSE" | tail -1)" "POST /public/tickets/"
TICKET=$(echo "$RESPONSE" | head -1 | pick "['public_id']")
echo "   ticket: $TICKET"

echo "== 2. The same submission again: idempotency (ADR-15) =="
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/public/tickets/" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $RUN_KEY" -d "$TICKET_BODY")
expect 200 "$STATUS" "an identical resubmission returns the original ticket"

echo "== 3. Sign in =="
TOKEN=$(curl -s -X POST "$API/auth/token/" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$AGENT_EMAIL\",\"password\":\"$PASSWORD\"}" | pick "['access']")
AUTH="Authorization: Bearer $TOKEN"
echo "   role: $(curl -s "$API/me/" -H "$AUTH" | pick "['role']")"

echo "== 4. The backend publishes the legal transitions =="
echo "   allowed_transitions: $(curl -s "$API/tickets/$TICKET/" -H "$AUTH" | pick "['allowed_transitions']")"

echo "== 5. Take the ticket and start working it =="
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/tickets/$TICKET/assign-to-me/" -H "$AUTH")
expect 200 "$STATUS" "POST /assign-to-me/"
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/tickets/$TICKET/status/" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"status":"IN_PROGRESS","note":"Picked up from the queue."}')
expect 200 "$STATUS" "POST /status/ IN_PROGRESS"

echo "== 6. Illegal transition: 400, and the set that was legal =="
RESPONSE=$(curl -s -w '\n%{http_code}' -X POST "$API/tickets/$TICKET/status/" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"status":"CLOSED"}')
expect 400 "$(echo "$RESPONSE" | tail -1)" "IN_PROGRESS -> CLOSED refused"
echo "$RESPONSE" | head -1 | pick "['error']['details']" | sed 's/^/   /'

echo "== 7. Stale If-Match: 409 version_conflict (ADR-07) =="
RESPONSE=$(curl -s -w '\n%{http_code}' -X POST "$API/tickets/$TICKET/status/" -H "$AUTH" \
  -H 'Content-Type: application/json' -H 'If-Match: 1' -d '{"status":"RESOLVED"}')
expect 409 "$(echo "$RESPONSE" | tail -1)" "write built on a stale version"
echo "$RESPONSE" | head -1 | pick "['error']['details']" | sed 's/^/   /'

echo "== 8. Internal comment and the unified timeline (ADR-08) =="
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/tickets/$TICKET/comments/" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"body":"Reproduced on staging: the reset token TTL is 15 minutes, not 24 hours."}')
expect 201 "$STATUS" "POST /comments/"
curl -s "$API/tickets/$TICKET/timeline/" -H "$AUTH" | python3 -c "
import json, sys
for entry in json.load(sys.stdin)['results']:
    actor = (entry['actor'] or {}).get('display_name', '-')
    print('   {:<8}{:<16}{} -> {:<14}{}'.format(
        entry['kind'], entry['type'], entry['old_value'] or '-', entry['new_value'] or '-', actor))"

echo "== 9. What the customer sees: nothing internal (ADR-03, ADR-17) =="
curl -s "$API/public/tickets/$TICKET/" | python3 -m json.tool | sed 's/^/   /'

echo "== 10. An agent does not touch a colleague's ticket =="
OTHER=$(curl -s -X POST "$API/auth/token/" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$OTHER_AGENT_EMAIL\",\"password\":\"$PASSWORD\"}" | pick "['access']")
RESPONSE=$(curl -s -w '\n%{http_code}' -X POST "$API/tickets/$TICKET/status/" \
  -H "Authorization: Bearer $OTHER" -H 'Content-Type: application/json' -d '{"status":"RESOLVED"}')
expect 403 "$(echo "$RESPONSE" | tail -1)" "another agent tries to change the status"
echo "   $(echo "$RESPONSE" | head -1 | pick "['error']['message']")"

echo "== 11. Tickets are never deleted (ADR-13) =="
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/tickets/$TICKET/" -H "$AUTH")
expect 405 "$STATUS" "DELETE /tickets/{uuid}/"

echo
echo "All good. Ticket used by this run: $API/tickets/$TICKET/"
