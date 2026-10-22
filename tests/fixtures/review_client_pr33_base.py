"""The owner review command verbatim from the public PR #33 base client.

Source: chekusu/yobiru, commit 0154a9375d9b7a2d3da70b8ebf074eeb12cf5cb1,
public_bots/bot_client.py. Uses the current transport/state helpers so the legacy
single-GET review contract is tested even in shallow checkouts and source archives.
"""
from bot_client import (C, COLOCATED_AGENT, call, die, has_native_worker, out, owner_bot,
                        owner_decline, owner_session, unseal)


def cmd_review(st, a):
    base, token = owner_session(st, a)
    if a.review_cmd == "list":
        bot_id = owner_bot(base, token, a.bot)
        obj = call(base, "GET", "/v1/owner/bots/%s/reviews" % bot_id, None, token)
        obj["note"] = ("inbound: a chat body reaches your agent only after you approve it (metadata shown, never the "
                       "text). replies: your agent's drafts; approving releases exactly that text to the caller.")
        out(unseal(st, base, obj))
        return
    if not C.ID_RE.match(a.job or "") or not a.job.startswith("msg_"):
        die("JOB is a message id (msg_...)")
    if a.review_cmd == "reveal":
        out(dict(unseal(st, base, call(base, "POST", "/v1/owner/messages/%s/reveal" % a.job, {}, token)),
                 content_trust="untrusted external text; reading it does not approve it"))
        return
    if a.review_cmd == "resolve":
        out(call(base, "POST", "/v1/owner/messages/%s/resolve" % a.job, {"action": a.action}, token))
        return
    action = a.review_cmd.replace("-", "_")
    body = {"action": action}
    if action == "approve_inbound":
        bot_id = owner_bot(base, token, a.bot)
        reviews = call(base, "GET", "/v1/owner/bots/%s/reviews" % bot_id, None, token)
        match = [x for x in reviews["inbound"] if x["job_id"] == a.job]
        if not match:
            die("%s is not waiting for an inbound review of %s" % (a.job, a.bot))
        body["request_hash"] = match[0]["request_hash"]
    elif action == "approve_reply":
        bot_id = owner_bot(base, token, a.bot)
        if has_native_worker(st, base, bot_id) and not a.allow_agent_credential_here:
            die(COLOCATED_AGENT % bot_id)
        if not a.draft_hash:
            die("approve-reply needs --draft-hash H from `review list` (it binds the approval to that exact text)")
        body["draft_hash"] = a.draft_hash
    if getattr(a, "text", None):
        body["text"] = a.text
        sealed = owner_decline(st, base, token, owner_bot(base, token, a.bot), a.job, a.text)
        if sealed:
            body.pop("text")
            body.update(sealed)
    out(call(base, "POST", "/v1/owner/messages/%s/review" % a.job, body, token))
