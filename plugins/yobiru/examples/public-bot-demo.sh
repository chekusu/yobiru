#!/usr/bin/env bash
# Yobiru public bot contacts: a local demo with synthetic data, on 127.0.0.1 only.
#
#   sh "<plugin>/examples/public-bot-demo.sh"
#
# Uses the yobiru command of the plugin this file belongs to (../scripts/yobiru). In one new temporary
# directory it gives the gateway, the owner's device, a caller, the owner's worker device and the owner's
# private calendar capability service their own directories, then runs the flow described in this plugin's
# docs/PUBLIC_BOTS.md. The gateway and the calendar service listen on 127.0.0.1 ports chosen by the
# operating system, and the calendar dates are computed from today. Bot, caller and message ids are read
# from each command's JSON output. The demo never uses Tailscale, an existing Yobiru data home, a real
# calendar or any address other than 127.0.0.1. When it ends, even on an error or Ctrl-C, it stops the
# services it started and deletes the temporary directory. It stops with an error if a step does not
# behave as described. Needs bash and Python 3.8+; written for Linux and macOS, tested on Linux.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
Y="$HERE/../scripts/yobiru"
[ -f "$Y" ] || { echo "demo: $Y is missing; run this file from inside the yobiru plugin" >&2; exit 2; }
PY=${YOBIRU_PYTHON:-python3}
T=$(mktemp -d "${TMPDIR:-/tmp}/yobiru-public-demo.XXXXXX")
trap 'kill $(jobs -p) 2>/dev/null || true; wait 2>/dev/null || true; rm -rf "$T"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TAILMSG_HOME="$T/unused-default-home"      # every command below names its own directory anyway

# j PATH...: values from the JSON on stdin; PATH is dotted keys and list indexes, * means every list item
j() {
  "$PY" -c 'import json, sys
def get(v, path):
    for i, k in enumerate(path):
        if k == "*":
            return [get(x, path[i + 1:]) for x in v]
        v = v[int(k)] if isinstance(v, list) else v[k]
    return v
d = json.load(sys.stdin)
print(" ".join(x if isinstance(x, str) else json.dumps(x) for x in (get(d, p.split(".")) for p in sys.argv[1:])))' "$@"
}

# P DEVICE ARGS...: `yobiru --home <temporary>/DEVICE public ARGS`; prints its JSON, or stops the demo
P() {
  local device=$1 out
  shift
  if ! out=$(sh "$Y" --home "$T/$device" public "$@"); then
    echo "demo: 'yobiru public $*' failed on the $device device: $out" >&2
    return 1
  fi
  printf '%s\n' "$out"
}

# listening NAME PID: the URL from the {"listening": URL} line that a service started in the background
# prints once it serves; gives up after 30 seconds or when the service exits
listening() {
  local n=0
  until grep -q '"listening"' "$T/$1.out" 2>/dev/null; do
    n=$((n + 1))
    if [ "$n" -gt 300 ] || ! kill -0 "$2" 2>/dev/null; then
      echo "demo: the $1 service did not start: $(tail -n 5 "$T/$1.log")" >&2
      return 1
    fi
    sleep 0.1
  done
  j listening < "$T/$1.out"
}

# expect WHAT ACTUAL WANTED: stop the demo unless a step behaved as described
expect() {
  [ "$2" = "$3" ] || { echo "demo: unexpected $1: $2 (expected: $3)" >&2; exit 1; }
}

echo "Yobiru public bot demo: synthetic data on 127.0.0.1 only, in $T (deleted at the end)"

# 1. The gateway: a separate mailbox service with its own directory, never the private receiver (7766).
PORT=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
sh "$Y" gateway --gateway-home "$T/gateway" init --port "$PORT" > /dev/null    # writes gateway/owner.token
sh "$Y" gateway --gateway-home "$T/gateway" serve > "$T/gateway.out" 2> "$T/gateway.log" &
URL=$(listening gateway $!)
echo "1. gateway listening on $URL"

# 2. The owner's device logs in with the owner credential, then creates and publishes the bot.
P owner owner login "$URL" --token-file "$T/gateway/owner.token" > /dev/null
BOT_ID=$(P owner bot create --handle assistant --name "Demo assistant" | j bot_id)
P owner bot publish assistant > /dev/null
echo "2. owner published $BOT_ID as $URL/@assistant"

# 3. A caller (no Tailscale) enrolls a pseudonymous identity, adds the bot and asks it a question.
ACTOR_ID=$(P caller enroll "$URL" --name "Demo caller" | j actor_id)
P caller add "$URL/@assistant" > /dev/null
FAQ_ID=$(P caller chat assistant "What can you do?" --faq | j message_id)
STATE=$(P caller status "$FAQ_ID" | j state)
expect "state of the first question" "$STATE" queued
echo "3. caller $ACTOR_ID asked a visitor question: $STATE (stored, not answered: no worker runs yet)"

# 4. The caller asks for more; the owner approves with an explicit list of capabilities (an explicit grant).
P caller request-friend assistant --note "Hello from the demo" > /dev/null
PENDING=$(P owner owner contacts assistant --pending | j contacts.0.actor_id)
expect "pending contact" "$PENDING" "$ACTOR_ID"
P owner owner contact assistant "$PENDING" approve --capabilities faq,calendar_slots > /dev/null
echo "4. owner granted $PENDING faq and calendar_slots explicitly"

# 5. The owner's private calendar service: owner rules, synthetic busy events two days from today, and
#    its own per-bot verifier credential. Event details stay in this service's directory.
DAY=$("$PY" -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(days=2)).date())')
sh "$Y" calendar-service init --dir "$T/calendar" --gateway "$URL" --bot "$BOT_ID" > /dev/null
P owner verifier bind assistant --token-file "$T/calendar/verifier.token" > /dev/null
"$PY" - "$T/calendar" "$DAY" <<'PY'
import json, os, sys
d, day = sys.argv[1], sys.argv[2]
with open(d + "/service.json") as f:
    cfg = json.load(f)
cfg["policy"].update(days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"], hours=[["09:00", "17:00"]],
                     durations=[60], min_notice_minutes=0, step_minutes=60, buffer_minutes=0)
with open(d + "/service.json", "w") as f:
    json.dump(cfg, f, indent=2)
events = [{"start": day + "T09:00:00Z", "end": day + "T10:00:00Z", "title": "SYNTHETIC-PRIVATE planning",
           "location": "SYNTHETIC-PRIVATE room", "attendees": ["SYNTHETIC-PRIVATE person"]},
          {"start": day + "T10:30:00Z", "end": day + "T11:00:00Z", "title": "SYNTHETIC-PRIVATE call"}]
fd = os.open(d + "/events.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"events": events}, f, indent=2)
PY
sh "$Y" calendar-service serve --dir "$T/calendar" > "$T/calendar.out" 2> "$T/calendar.log" &
CAL_URL=$(listening calendar $!)
echo "5. calendar service on $CAL_URL; synthetic busy times on $DAY: 09:00-10:00 and 10:30-11:00 UTC"

# 6. The caller asks for meeting times and sends a message while the owner's worker is still offline.
SLOTS_ID=$(P caller slots assistant --duration 60 --after "$DAY" --before "$DAY" | j message_id)
NOTE_ID=$(P caller chat assistant "Please tell the owner hello" | j message_id)
STATES=$(P caller history assistant | j messages.*.state)
expect "states before the worker runs" "$STATES" '["queued", "queued", "queued"]'
echo "6. caller asked for times on $DAY and sent a message; all three messages: $STATES"

# 7. The worker device gets its own per-bot worker credential (never the owner's), the deterministic
#    responder connected to the calendar service, and makes one pass over the queued work. The message is
#    not for the worker: it is a direct message for the owner's own agent.
P owner worker bind assistant --token-file "$T/worker.token" > /dev/null
P worker worker login "$URL" --token-file "$T/worker.token" --handle @assistant > /dev/null
P worker responder init "$BOT_ID" --calendar-url "$CAL_URL" --calendar-token-file "$T/calendar/access.token" > /dev/null
OUTCOMES=$(P worker worker run "$BOT_ID" --once | j processed.*.outcome)
expect "worker outcomes" "$OUTCOMES" '["completed", "completed"]'
echo "7. one worker pass: $OUTCOMES"

# 8. What the caller has now, and the message in the owner's message requests (the caller is not in the inbox).
ANSWER=$(P caller status "$FAQ_ID" | j result.text)
SLOTS=$(P caller status "$SLOTS_ID" | j result.slots.*.start)
expect "offered times" "$SLOTS" "[\"${DAY}T11:00:00Z\", \"${DAY}T12:00:00Z\", \"${DAY}T13:00:00Z\"]"
NOTE=$(P caller status "$NOTE_ID" | j state)
expect "the message's state" "$NOTE" queued
REQUEST=$(P owner review list assistant | j inbound.*.message.text)
expect "message request" "$REQUEST" '["Please tell the owner hello"]'
echo "8. caller got the FAQ answer: $ANSWER"
echo "   offered times (nothing booked): $SLOTS"
echo "   the message: $NOTE, in the owner's message requests: $REQUEST (no message needs a reply)"

# 9. Where the synthetic private event details ended up, then the owner revokes the caller.
FOUND=$(cd "$T" && grep -rl SYNTHETIC-PRIVATE . | sort)
expect "files with private event details" "$FOUND" ./calendar/events.json
P owner owner contact assistant "$ACTOR_ID" revoke > /dev/null
AFTER=$(sh "$Y" --home "$T/caller" public history assistant | j error http_status || true)
expect "caller read after revocation" "$AFTER" "not_found 404"
echo "9. private event details are only in: $FOUND"
echo "   owner revoked the caller; the caller's next history read: $AFTER"
echo "Demo finished: every step behaved as described. Stopping the services and deleting $T."
