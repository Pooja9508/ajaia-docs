"""
Email service — uses Python's built-in smtplib.
Configure via environment variables (see .env.example).
If SMTP is not configured, send_email() returns False and the caller
shows the invite link in the UI instead.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _cfg():
    return {
        "host":      os.getenv("SMTP_HOST", ""),
        "port":      int(os.getenv("SMTP_PORT", "587")),
        "user":      os.getenv("SMTP_USER", ""),
        "password":  os.getenv("SMTP_PASS", ""),
        "from_addr": os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "noreply@ajaia.app"),
    }


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Return True if email was sent, False if SMTP not configured or failed."""
    cfg = _cfg()
    if not cfg["host"] or not cfg["user"] or not cfg["password"]:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["from_addr"]
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.sendmail(cfg["from_addr"], to, msg.as_string())
        return True
    except Exception as exc:
        print(f"[email] send failed to {to}: {exc}")
        return False


# ── HTML templates ────────────────────────────────────────────────────────────

def _base(content: str, app_name: str = "Ajaia Docs") -> str:
    return f"""
    <div style="font-family:Inter,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;background:#f8fafc;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <span style="font-size:20px;font-weight:800;color:#1e3a8a">{app_name}</span>
      </div>
      {content}
      <div style="margin-top:32px;font-size:11px;color:#94a3b8;text-align:center">
        This email was sent by {app_name}. If you didn't expect it, you can ignore it.
      </div>
    </div>"""


def share_notification_email(sharer: str, doc_title: str, app_url: str, permission: str) -> str:
    return _base(f"""
      <p style="font-size:15px;color:#1e293b">
        <strong>{sharer}</strong> has shared a document with you on Ajaia Docs.
      </p>
      <div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:16px 0">
        <div style="font-weight:600;color:#0f172a;margin-bottom:4px">📄 {doc_title}</div>
        <div style="font-size:12px;color:#64748b">Your access: <strong>{permission}</strong></div>
      </div>
      <a href="{app_url}" style="display:inline-block;background:#4f86e8;color:#fff;padding:10px 20px;border-radius:8px;font-weight:600;text-decoration:none">
        Open Document
      </a>""")


def invite_email(inviter: str, doc_title: str, invite_link: str, permission: str) -> str:
    return _base(f"""
      <p style="font-size:15px;color:#1e293b">
        <strong>{inviter}</strong> invited you to collaborate on a document.
      </p>
      <div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:16px 0">
        <div style="font-weight:600;color:#0f172a;margin-bottom:4px">📄 {doc_title}</div>
        <div style="font-size:12px;color:#64748b">Your access: <strong>{permission}</strong></div>
      </div>
      <p style="color:#475569;font-size:13px">Click below to accept. You'll be asked to create an account if you don't have one.</p>
      <a href="{invite_link}" style="display:inline-block;background:#4f86e8;color:#fff;padding:10px 20px;border-radius:8px;font-weight:600;text-decoration:none">
        Accept Invitation
      </a>
      <p style="margin-top:16px;font-size:11px;color:#94a3b8">Or copy this link: {invite_link}</p>""")


def access_request_email(requester: str, requester_email: str, doc_title: str, requested_perm: str, message: str, app_url: str) -> str:
    msg_block = f'<p style="color:#475569;font-size:13px;font-style:italic">"{message}"</p>' if message else ""
    return _base(f"""
      <p style="font-size:15px;color:#1e293b">
        <strong>{requester}</strong> ({requester_email}) is requesting
        <strong>{requested_perm}</strong> access to your document.
      </p>
      <div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:16px 0">
        <div style="font-weight:600;color:#0f172a">📄 {doc_title}</div>
      </div>
      {msg_block}
      <a href="{app_url}app" style="display:inline-block;background:#4f86e8;color:#fff;padding:10px 20px;border-radius:8px;font-weight:600;text-decoration:none">
        Review Request in Ajaia Docs
      </a>""")


def access_decision_email(doc_title: str, approved: bool) -> str:
    color  = "#065f46" if approved else "#7f1d1d"
    status = "approved" if approved else "denied"
    icon   = "✅" if approved else "❌"
    return _base(f"""
      <p style="font-size:15px;color:#1e293b">
        Your access request has been <strong style="color:{color}">{status}</strong>.
      </p>
      <div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:16px 0">
        <div>{icon} <strong>{doc_title}</strong></div>
      </div>""")
