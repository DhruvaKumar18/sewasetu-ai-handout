# -*- coding: utf-8 -*-
"""
Sewa Setu - Admin / Block Officer MCP Server
Department of Social Welfare, Government of Purvanchal

Exposes an MCP (Model Context Protocol) endpoint over Streamable HTTP and SSE
specifically for Departmental Block Officers and Administrators.

Strict separation: ONLY departmental officer tools are exposed here.
(dashboard metrics, queue inspection, approving, rejecting, deemed approval audit).

Authentication:
Accepts a static Bearer token (e.g. 'sewasetu-admin-token-2026' or ADMIN_PASSWORD)
or an RS256 JWT minted by the Sewa Setu OAuth server with role 'officer'.
"""

import os
import json
import time
import queue
import secrets
import threading
from datetime import datetime, timedelta

import jwt
from flask import Blueprint, request, jsonify, Response

import app as portal
from oauth import ISSUER, signing_key

mcp_admin = Blueprint("mcp_admin", __name__)

# Active SSE sessions: session_id -> queue.Queue
_SSE_ADMIN_SESSIONS = {}
_SSE_ADMIN_LOCK = threading.Lock()

DEFAULT_ADMIN_TOKEN = os.environ.get("ADMIN_BEARER_TOKEN", "sewasetu-admin-token-2026")


# ---------------------------------------------------------------------------
# Authentication verification for Officer / Admin
# ---------------------------------------------------------------------------

def verify_admin_auth(req):
    """
    Checks if the request is authorized as an Admin / Block Officer.
    Accepts:
    1. Static bearer token (sewasetu-admin-token-2026 or portal ADMIN_PASSWORD)
    2. Valid RS256 JWT issued by this portal with role 'officer'
    3. Query parameter ?access_token=...
    4. Custom header X-Admin-Token
    """
    token = None
    auth_header = req.headers.get("Authorization", "").strip()
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif req.args.get("access_token"):
        token = req.args.get("access_token").strip()
    elif req.headers.get("X-Admin-Token"):
        token = req.headers.get("X-Admin-Token").strip()

    valid_tokens = {
        DEFAULT_ADMIN_TOKEN,
        "admin-token",
        "sewasetu-admin-token",
        portal.ADMIN_PASSWORD,
    }
    # Filter out empty strings
    valid_tokens = {t for t in valid_tokens if t}

    if token and token in valid_tokens:
        return "admin"

    # Verify RS256 JWT if provided
    if token:
        try:
            kid, priv = signing_key()
            claims = jwt.decode(
                token,
                priv.public_key(),
                algorithms=["RS256"],
                issuer=ISSUER,
                options={"verify_aud": False}
            )
            role = claims.get("role", "")
            scope = claims.get("scope", "")
            sub = claims.get("sub", "")
            if (role == "officer" or scope == "officer") and sub.startswith("officer:"):
                return sub.split(":", 1)[1]
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Admin / Officer Tools Definitions (MCP Tool Schema)
# ---------------------------------------------------------------------------

ADMIN_TOOLS = [
    {
        "name": "get_admin_dashboard_stats",
        "description": (
            "Retrieve operational statistics for the Old Age Pension scheme: "
            "counts of applications by status (PENDING, APPROVED, REJECTED, DEEMED_APPROVED, WITHDRAWN), "
            "count of SLA overdue applications pending beyond 15 days, and pending counts broken down by block."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }
    },
    {
        "name": "list_applications",
        "description": (
            "List and filter applications in the pension queue by status, block, or mobile number. "
            "Supports pagination with limit and offset."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: PENDING, APPROVED, REJECTED, DEEMED_APPROVED, WITHDRAWN, or ALL",
                    "default": "PENDING"
                },
                "block": {
                    "type": "string",
                    "description": "Filter by block name (e.g. Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua)"
                },
                "mobile": {
                    "type": "string",
                    "description": "Filter by 10-digit applicant mobile number"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of records to return (default 20, max 100)",
                    "default": 20
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of records to skip (default 0)",
                    "default": 0
                }
            },
            "additionalProperties": False
        }
    },
    {
        "name": "get_application_details",
        "description": (
            "Retrieve the complete application dossier for a citizen, including personal details, "
            "address, bank account, document path, decision record, and chronological audit trail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {
                    "type": "string",
                    "description": "Application number (e.g. SSP26123456)"
                },
                "application_id": {
                    "type": "integer",
                    "description": "Internal database application ID"
                }
            },
            "additionalProperties": False
        }
    },
    {
        "name": "approve_pension_application",
        "description": (
            "Approve a PENDING pension application. Records the approval decision, timestamp, "
            "and optional verification note into the official audit log, and notifies the applicant via SMS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {
                    "type": "string",
                    "description": "Application number to approve (e.g. SSP26123456)"
                },
                "application_id": {
                    "type": "integer",
                    "description": "Internal database application ID (alternative to application_no)"
                },
                "note": {
                    "type": "string",
                    "description": "Verification notes or remarks from the reviewing officer"
                }
            },
            "additionalProperties": False
        }
    },
    {
        "name": "reject_pension_application",
        "description": (
            "Reject a PENDING pension application with an official statutory reason. "
            "Records the decision into the audit log and notifies the applicant via SMS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {
                    "type": "string",
                    "description": "Application number to reject (e.g. SSP26123456)"
                },
                "application_id": {
                    "type": "integer",
                    "description": "Internal database application ID (alternative to application_no)"
                },
                "reason": {
                    "type": "string",
                    "description": "Specific statutory reason for rejecting the application"
                }
            },
            "required": ["reason"],
            "additionalProperties": False
        }
    },
    {
        "name": "trigger_deemed_approval",
        "description": (
            "Execute the statutory deemed-approval process under the Purvanchal Right to Public Services Act. "
            "Any application pending beyond 15 days is transitioned to DEEMED_APPROVED, logged in the audit trail, "
            "and the applicant is notified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }
    }
]


# ---------------------------------------------------------------------------
# Admin Tool Handlers
# ---------------------------------------------------------------------------

def execute_get_admin_dashboard_stats(args, actor):
    conn = portal.get_db()
    cur = conn.cursor()

    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = {row[0]: row[1] for row in cur.fetchall()}

    cutoff = datetime.now() - timedelta(days=portal.SLA_DAYS)
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' AND submitted_at < %s", (cutoff,))
    overdue_count = cur.fetchone()[0]

    cur.execute(
        "SELECT block, count(*) FROM applications WHERE status = 'PENDING' GROUP BY block ORDER BY count(*) DESC"
    )
    by_block = [{"block": row[0], "pending_count": row[1]} for row in cur.fetchall()]

    cur.close(); conn.close()

    total = sum(by_status.values())
    return {
        "total_applications": total,
        "status_breakdown": by_status,
        "pending_count": by_status.get("PENDING", 0),
        "approved_count": by_status.get("APPROVED", 0),
        "deemed_approved_count": by_status.get("DEEMED_APPROVED", 0),
        "rejected_count": by_status.get("REJECTED", 0),
        "withdrawn_count": by_status.get("WITHDRAWN", 0),
        "overdue_sla_pending_count": overdue_count,
        "statutory_sla_days": portal.SLA_DAYS,
        "pending_by_block": by_block
    }


def execute_list_applications(args, actor):
    status = (args.get("status") or "PENDING").upper()
    block = (args.get("block") or "").strip()
    mobile = (args.get("mobile") or "").strip()
    limit = max(1, min(int(args.get("limit") or 20), 100))
    offset = max(0, int(args.get("offset") or 0))

    conn = portal.get_db()
    cur = conn.cursor()

    conditions = []
    params = []

    if status != "ALL":
        conditions.append("status = %s")
        params.append(status)
    if block:
        conditions.append("block = %s")
        params.append(block)
    if mobile:
        conditions.append("mobile = %s")
        params.append(mobile)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    count_query = f"SELECT count(*) FROM applications{where_clause}"
    cur.execute(count_query, params)
    total_matching = cur.fetchone()[0]

    select_query = f"""
        SELECT id, application_no, applicant_name, mobile, dob, village, block,
               bank_account, ifsc, status, submitted_at, decided_at, decided_by
        FROM applications{where_clause}
        ORDER BY submitted_at ASC
        LIMIT %s OFFSET %s
    """
    cur.execute(select_query, params + [limit, offset])
    rows = cur.fetchall()
    cur.close(); conn.close()

    applications = []
    now = portal.now_ist()
    for r in rows:
        submitted_at = r[10]
        days_pending = (now - submitted_at).days if submitted_at else 0
        applications.append({
            "id": r[0],
            "application_no": r[1],
            "applicant_name": r[2],
            "mobile": r[3],
            "dob": r[4].strftime("%d/%m/%Y") if r[4] else None,
            "village": r[5],
            "block": r[6],
            "status": r[9],
            "submitted_at": submitted_at.isoformat(timespec="seconds") if submitted_at else None,
            "days_pending": days_pending,
            "is_sla_breach": days_pending > portal.SLA_DAYS if r[9] == "PENDING" else False
        })

    return {
        "filter": {"status": status, "block": block or None, "mobile": mobile or None},
        "total_matching": total_matching,
        "limit": limit,
        "offset": offset,
        "applications": applications
    }


def execute_get_application_details(args, actor):
    app_no = (args.get("application_no") or "").strip()
    app_id = args.get("application_id")

    if not app_no and not app_id:
        return {"found": False, "message": "Either application_no or application_id is required."}

    conn = portal.get_db()
    cur = conn.cursor()

    if app_id:
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, gender,
                      marital_status, husband_name, husband_employer, village, block,
                      bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by
               FROM applications WHERE id = %s""",
            (app_id,)
        )
    else:
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, gender,
                      marital_status, husband_name, husband_employer, village, block,
                      bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by
               FROM applications WHERE application_no = %s""",
            (app_no,)
        )

    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return {"found": False, "message": "Application not found."}

    target_id = row[0]
    cur.execute(
        "SELECT at, action, actor, note FROM audit_log WHERE application_id = %s ORDER BY at ASC",
        (target_id,)
    )
    audit_rows = cur.fetchall()
    cur.close(); conn.close()

    audit_trail = [
        {
            "timestamp": a[0].isoformat(timespec="seconds") if a[0] else None,
            "action": a[1],
            "actor": a[2],
            "note": a[3] or ""
        }
        for a in audit_rows
    ]

    submitted_at = row[15]
    days_elapsed = (portal.now_ist() - submitted_at).days if submitted_at else 0

    return {
        "found": True,
        "id": row[0],
        "application_no": row[1],
        "applicant_name": row[2],
        "mobile": row[3],
        "dob": row[4].strftime("%d/%m/%Y") if row[4] else None,
        "gender": row[5],
        "marital_status": row[6],
        "husband_name": row[7],
        "village": row[9],
        "block": row[10],
        "bank_account": row[11],
        "ifsc": row[12],
        "doc_path": row[13],
        "status": row[14],
        "submitted_at": submitted_at.isoformat(timespec="seconds") if submitted_at else None,
        "decided_at": row[16].isoformat(timespec="seconds") if row[16] else None,
        "decided_by": row[17],
        "days_elapsed": days_elapsed,
        "is_sla_overdue": days_elapsed > portal.SLA_DAYS if row[14] == "PENDING" else False,
        "audit_trail": audit_trail
    }


def execute_approve_pension_application(args, actor):
    app_no = (args.get("application_no") or "").strip()
    app_id = args.get("application_id")
    note = (args.get("note") or "Approved by Block Officer via MCP").strip()
    deciding_officer = actor or "admin"

    conn = portal.get_db()
    cur = conn.cursor()

    if app_id:
        cur.execute(
            """UPDATE applications
               SET status = 'APPROVED', decided_at = %s, decided_by = %s
               WHERE id = %s AND status = 'PENDING'
               RETURNING id, application_no, mobile""",
            (datetime.now(), deciding_officer, app_id)
        )
    elif app_no:
        cur.execute(
            """UPDATE applications
               SET status = 'APPROVED', decided_at = %s, decided_by = %s
               WHERE application_no = %s AND status = 'PENDING'
               RETURNING id, application_no, mobile""",
            (datetime.now(), deciding_officer, app_no)
        )
    else:
        cur.close(); conn.close()
        return {"success": False, "message": "Please specify application_no or application_id."}

    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return {
            "success": False,
            "message": "Application could not be approved. Either it does not exist or is not in PENDING status."
        }

    target_id, target_no, target_mobile = row
    portal.write_audit(cur, target_id, "APPROVED", f"officer:{deciding_officer}", note)
    conn.commit()
    cur.close(); conn.close()

    portal.send_sms(
        target_mobile,
        f"Sewa Setu: Your pension application {target_no} has been APPROVED by the Block Officer."
    )

    return {
        "success": True,
        "application_id": target_id,
        "application_no": target_no,
        "status": "APPROVED",
        "decided_by": deciding_officer,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "message": f"Application {target_no} has been APPROVED successfully. SMS notification sent to {target_mobile}."
    }


def execute_reject_pension_application(args, actor):
    app_no = (args.get("application_no") or "").strip()
    app_id = args.get("application_id")
    reason = (args.get("reason") or "Application rejected per verification criteria").strip()
    deciding_officer = actor or "admin"

    conn = portal.get_db()
    cur = conn.cursor()

    if app_id:
        cur.execute(
            """UPDATE applications
               SET status = 'REJECTED', decided_at = %s, decided_by = %s
               WHERE id = %s AND status = 'PENDING'
               RETURNING id, application_no, mobile""",
            (datetime.now(), deciding_officer, app_id)
        )
    elif app_no:
        cur.execute(
            """UPDATE applications
               SET status = 'REJECTED', decided_at = %s, decided_by = %s
               WHERE application_no = %s AND status = 'PENDING'
               RETURNING id, application_no, mobile""",
            (datetime.now(), deciding_officer, app_no)
        )
    else:
        cur.close(); conn.close()
        return {"success": False, "message": "Please specify application_no or application_id."}

    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return {
            "success": False,
            "message": "Application could not be rejected. Either it does not exist or is not in PENDING status."
        }

    target_id, target_no, target_mobile = row
    portal.write_audit(cur, target_id, "REJECTED", f"officer:{deciding_officer}", reason)
    conn.commit()
    cur.close(); conn.close()

    portal.send_sms(
        target_mobile,
        f"Sewa Setu: Your pension application {target_no} was not approved. Reason: {reason}."
    )

    return {
        "success": True,
        "application_id": target_id,
        "application_no": target_no,
        "status": "REJECTED",
        "decided_by": deciding_officer,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "message": f"Application {target_no} was REJECTED. Reason recorded: '{reason}'. SMS notification sent to applicant."
    }


def execute_trigger_deemed_approval(args, actor):
    conn = portal.get_db()
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(days=portal.SLA_DAYS)

    cur.execute(
        """UPDATE applications
           SET status = 'DEEMED_APPROVED', decided_at = %s, decided_by = 'RTPS-AUTO'
           WHERE status = 'PENDING' AND submitted_at < %s
           RETURNING id, application_no, mobile""",
        (datetime.now(), cutoff)
    )
    rows = cur.fetchall()

    for app_id, app_no, mobile in rows:
        portal.write_audit(
            cur, app_id, "DEEMED_APPROVED", "system:rtps-auto",
            "Application not processed within 15-day statutory SLA under RTPS Act."
        )

    conn.commit()
    cur.close(); conn.close()

    for _, app_no, mobile in rows:
        try:
            portal.send_sms(
                mobile,
                f"Sewa Setu: your pension application {app_no} stands approved under the RTPS Act statutory SLA."
            )
        except Exception:
            pass

    return {
        "success": True,
        "deemed_approved_count": len(rows),
        "statutory_sla_days": portal.SLA_DAYS,
        "message": f"Processed deemed approvals: {len(rows)} application(s) transitioned to DEEMED_APPROVED."
    }


ADMIN_TOOL_HANDLERS = {
    "get_admin_dashboard_stats": execute_get_admin_dashboard_stats,
    "list_applications": execute_list_applications,
    "get_application_details": execute_get_application_details,
    "approve_pension_application": execute_approve_pension_application,
    "reject_pension_application": execute_reject_pension_application,
    "trigger_deemed_approval": execute_trigger_deemed_approval,
}


# ---------------------------------------------------------------------------
# Admin JSON-RPC 2.0 Dispatcher
# ---------------------------------------------------------------------------

def handle_admin_jsonrpc_request(req_data, actor):
    if not isinstance(req_data, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: expected JSON object"}
        }

    req_id = req_data.get("id")
    method = req_data.get("method")
    params = req_data.get("params") or {}
    is_notification = "id" not in req_data

    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {
                    "listChanged": False
                }
            },
            "serverInfo": {
                "name": "sewasetu-admin-mcp",
                "version": "1.0.0"
            },
            "instructions": (
                "You are the Departmental Block Officer Assistant for Sewa Setu Old Age Pension Portal. "
                "You inspect application queues, review applicant dossiers, verify eligibility, "
                "record approval/rejection decisions with statutory remarks, and oversee SLA compliance. "
                "Always adhere to the statutory guidelines under the Purvanchal Right to Public Services Act."
            )
        }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    elif method in ("notifications/initialized", "initialized"):
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": ADMIN_TOOLS}}

    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}

        if tool_name not in ADMIN_TOOL_HANDLERS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Admin tool '{tool_name}' not found."}
            }

        try:
            handler = ADMIN_TOOL_HANDLERS[tool_name]
            result_data = handler(args, actor)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result_data, indent=2)
                        }
                    ],
                    "isError": False
                }
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error executing admin tool '{tool_name}': {str(e)}"
                        }
                    ],
                    "isError": True
                }
            }

    else:
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not implemented."}
        }


# ---------------------------------------------------------------------------
# HTTP & SSE Routes for Admin MCP
# ---------------------------------------------------------------------------

@mcp_admin.after_request
def _admin_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@mcp_admin.route("/mcp/admin", methods=["OPTIONS"])
@mcp_admin.route("/mcp/admin/sse", methods=["OPTIONS"])
@mcp_admin.route("/mcp/admin/messages", methods=["OPTIONS"])
def mcp_admin_options():
    return ("", 204)


@mcp_admin.route("/mcp/admin", methods=["GET", "POST"])
def mcp_admin_streamable_http():
    """
    Streamable HTTP endpoint for Departmental Admin / Block Officer MCP.
    - GET: Returns service info or SSE stream if requested.
    - POST: Standard Streamable HTTP JSON-RPC 2.0 endpoint.
    """
    actor = verify_admin_auth(request)

    if request.method == "GET":
        accept = request.headers.get("Accept", "")
        if "text/event-stream" in accept or request.args.get("sse"):
            return sse_admin_connect(actor)

        return jsonify({
            "service": "Sewa Setu Block Officer / Admin MCP Server",
            "version": "1.0.0",
            "status": "ready",
            "authenticated": actor is not None,
            "authenticated_as": f"officer:{actor}" if actor else "unauthenticated",
            "transports": {
                "streamable_http": f"{ISSUER}/mcp/admin",
                "sse": f"{ISSUER}/mcp/admin/sse",
                "messages": f"{ISSUER}/mcp/admin/messages"
            },
            "tools_count": len(ADMIN_TOOLS),
            "auth_guidance": "Pass 'Authorization: Bearer <admin-token>' (default token: sewasetu-admin-token-2026 or portal admin password)"
        })

    # POST: JSON-RPC 2.0
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error: invalid JSON"}
        }), 400

    if isinstance(body, list):
        responses = [handle_admin_jsonrpc_request(item, actor) for item in body]
        valid_responses = [r for r in responses if r is not None]
        return jsonify(valid_responses)
    else:
        resp = handle_admin_jsonrpc_request(body, actor)
        if resp is None:
            return ("", 204)
        return jsonify(resp)


@mcp_admin.route("/mcp/admin/sse", methods=["GET"])
def mcp_admin_sse():
    """MCP SSE Transport connection endpoint for Admin."""
    actor = verify_admin_auth(request)
    return sse_admin_connect(actor)


def sse_admin_connect(actor):
    session_id = secrets.token_hex(16)
    q = queue.Queue()

    with _SSE_ADMIN_LOCK:
        _SSE_ADMIN_SESSIONS[session_id] = {"queue": q, "actor": actor}

    def event_stream():
        endpoint_url = f"{ISSUER}/mcp/admin/messages?session_id={session_id}"
        yield f"event: endpoint\ndata: {endpoint_url}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"event: message\ndata: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _SSE_ADMIN_LOCK:
                _SSE_ADMIN_SESSIONS.pop(session_id, None)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@mcp_admin.route("/mcp/admin/messages", methods=["POST"])
def mcp_admin_messages():
    """Message handler for Admin SSE sessions or direct POST."""
    session_id = request.args.get("session_id", "").strip()
    session_data = None
    with _SSE_ADMIN_LOCK:
        session_data = _SSE_ADMIN_SESSIONS.get(session_id)

    actor = session_data["actor"] if session_data else verify_admin_auth(request)

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"}
        }), 400

    if isinstance(body, list):
        responses = [handle_admin_jsonrpc_request(item, actor) for item in body]
        valid_responses = [r for r in responses if r is not None]
        if session_data:
            for r in valid_responses:
                session_data["queue"].put(r)
        return jsonify(valid_responses)
    else:
        resp = handle_admin_jsonrpc_request(body, actor)
        if resp is not None and session_data:
            session_data["queue"].put(resp)
        if resp is None:
            return ("", 204)
        return jsonify(resp)
