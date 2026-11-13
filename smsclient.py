"""
Tiny client for the SMSGate service (see ../smsgate). Pure standard library, never raises:
if SMSGate is not configured or is down, the portal keeps working exactly as before.

Environment variables:
  SMSGATE_URL         e.g. https://sms.yourcompany.com
  SMSGATE_SID         Account SID  (AC...)
  SMSGATE_TOKEN       Auth token
  SMS_NOTIFY_KINDS    notification kinds that are also sent by SMS (default: leave,warning,salary)
"""
import base64
import json
import os
import threading
import urllib.request


def enabled():
    return bool(os.environ.get("SMSGATE_URL") and os.environ.get("SMSGATE_SID") and os.environ.get("SMSGATE_TOKEN"))


def notify_kinds():
    return {k.strip() for k in os.environ.get("SMS_NOTIFY_KINDS", "leave,warning,salary").split(",") if k.strip()}


def _call(path, payload):
    try:
        cred = f"{os.environ['SMSGATE_SID']}:{os.environ['SMSGATE_TOKEN']}".encode()
        req = urllib.request.Request(
            os.environ["SMSGATE_URL"].rstrip("/") + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(cred).decode()})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:                      # HTTPError (429 etc.), timeout, DNS ...
        print("[smsclient]", path, "failed:", e, flush=True)
        return None


def send(to, body):
    return bool(enabled() and to and _call("/v1/messages", {"to": to, "body": body[:300]}))


def send_async(to, body):
    """Fire and forget - never blocks a web request or a database transaction."""
    if enabled() and to:
        threading.Thread(target=send, args=(to, body), daemon=True).start()


def verify_start_async(to, app_name="CyberAttend"):
    if enabled() and to:
        threading.Thread(target=_call, args=("/v1/verify/start", {"to": to, "app": app_name}), daemon=True).start()


def verify_check(to, code):
    if not (enabled() and to and code):
        return False
    r = _call("/v1/verify/check", {"to": to, "code": code})
    return bool(r and r.get("valid"))
