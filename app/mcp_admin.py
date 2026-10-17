# -*- coding: utf-8 -*-
"""
Sewa Setu - Admin / Block Officer MCP Server (Official MCP SDK Implementation)
Department of Social Welfare, Government of Purvanchal

Exposes a Model Context Protocol (MCP) server over Streamable HTTP and SSE
specifically for Departmental Block Officers and Administrators, powered by
the official Python MCP SDK (mcp.server.mcpserver).

Strict separation: ONLY departmental officer tools are exposed here
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
import asyncio
import contextvars
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import jwt
from flask import Blueprint, request, jsonify, Response
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import Tool, TextContent, CallToolResult

mcp_admin = Blueprint("mcp_admin", __name__)

import app as portal
from oauth import ISSUER, signing_key

# Active SSE sessions: session_id -> queue.Queue
_SSE_ADMIN_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SSE_ADMIN_LOCK = threading.Lock()

DEFAULT_ADMIN_TOKEN = os.environ.get("ADMIN_BEARER_TOKEN", "sewasetu-admin-token-2026")

# ContextVar to pass authenticated admin actor safely to tool execution
CURRENT_ADMIN_ACTOR: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "CURRENT_ADMIN_ACTOR", default=None
)

# Initialize official MCP SDK Server instance for Admin
admin_mcp = MCPServer(
    name="sewasetu-admin-mcp",
    instructions=(
        "You are the Departmental Block Officer Assistant for Sewa Setu Old Age Pension Portal. "
        "You inspect application queues, review applicant dossiers, verify eligibility, "
        "record approval/rejection decisions with statutory remarks, and oversee SLA compliance. "
        "Always adhere to the statutory guidelines under the Purvanchal Right to Public Services Act."
    ),
    version="1.0.0"
)


# ---------------------------------------------------------------------------
# Authentication verification for Officer / Admin
# ---------------------------------------------------------------------------

def verify_admin_auth(req) -> Optional[str]:
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
    valid_tokens = {t for t in valid_tokens if t}

    if token and token in valid_tokens:
        return "admin"

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
# Admin / Officer Tools Definitions (Registered with official MCP SDK)
# ---------------------------------------------------------------------------

@admin_mcp.tool(
    name="get_admin_dashboard_stats",
    description=(
        "Retrieve operational statistics for the Old Age Pension scheme: "
        "counts of applications by status (PENDING, APPROVED, REJECTED, DEEMED_APPROVED, WITHDRAWN), "
        "count of SLA overdue applications pending beyond 15 days, and pending counts broken down by block."
    )
)
def get_admin_dashboard_stats() -> dict:
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


@admin_mcp.tool(
    name="list_applications",
    description=(
        "List and filter applications in the pension queue by status, block, or mobile number. "
        "Supports pagination with limit and offset."
    )
)
def list_applications(
    status: Optional[str] = Field("PENDING", description="Filter by status: PENDING, APPROVED, REJECTED, DEEMED_APPROVED, WITHDRAWN, or ALL"),
    block: Optional[str] = Field(None, description="Filter by block name (e.g. Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua)"),
    mobile: Optional[str] = Field(None, description="Filter by 10-digit applicant mobile number"),
    limit: Optional[int] = Field(20, description="Number of records to return (default 20, max 100)"),
    offset: Optional[int] = Field(0, description="Number of records to skip (default 0)")
) -> dict:
    status_filter = (status or "PENDING").upper()
    block_filter = (block or "").strip()
    mobile_filter = (mobile or "").strip()
    eff_limit = max(1, min(int(limit or 20), 100))
    eff_offset = max(0, int(offset or 0))

    conn = portal.get_db()
    cur = conn.cursor()

    conditions = []
    params = []

    if status_filter != "ALL":
        conditions.append("status = %s")
        params.append(status_filter)
    if block_filter:
        conditions.append("block = %s")
        params.append(block_filter)
    if mobile_filter:
        conditions.append("mobile = %s")
        params.append(mobile_filter)

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
    cur.execute(select_query, params + [eff_limit, eff_offset])
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
        "filter": {"status": status_filter, "block": block_filter or None, "mobile": mobile_filter or None},
        "total_matching": total_matching,
        "limit": eff_limit,
        "offset": eff_offset,
        "applications": applications
    }


@admin_mcp.tool(
    name="get_application_details",
    description=(
        "Retrieve the complete application dossier for a citizen, including personal details, "
        "address, bank account, document path, decision record, and chronological audit trail."
    )
)
def get_application_details(
    application_no: Optional[str] = Field(None, description="Application number (e.g. SSP26123456)"),
    application_id: Optional[int] = Field(None, description="Internal database application ID")
) -> dict:
    app_no = (application_no or "").strip()
    app_id = application_id

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


@admin_mcp.tool(
    name="approve_pension_application",
    description=(
        "Approve a PENDING pension application. Records the approval decision, timestamp, "
        "and optional verification note into the official audit log, and notifies the applicant via SMS."
    )
)
def approve_pension_application(
    application_no: Optional[str] = Field(None, description="Application number to approve (e.g. SSP26123456)"),
    application_id: Optional[int] = Field(None, description="Internal database application ID (alternative to application_no)"),
    note: Optional[str] = Field("", description="Verification notes or remarks from the reviewing officer")
) -> dict:
    app_no = (application_no or "").strip()
    app_id = application_id
    eff_note = (note or "Approved by Block Officer via MCP").strip()
    deciding_officer = CURRENT_ADMIN_ACTOR.get() or "admin"

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
    portal.write_audit(cur, target_id, "APPROVED", f"officer:{deciding_officer}", eff_note)
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
        "note": eff_note,
        "message": f"Application {target_no} has been APPROVED successfully. SMS notification sent to {target_mobile}."
    }


@admin_mcp.tool(
    name="reject_pension_application",
    description=(
        "Reject a PENDING pension application with an official statutory reason. "
        "Records the decision into the audit log and notifies the applicant via SMS."
    )
)
def reject_pension_application(
    reason: str = Field(..., description="Specific statutory reason for rejecting the application"),
    application_no: Optional[str] = Field(None, description="Application number to reject (e.g. SSP26123456)"),
    application_id: Optional[int] = Field(None, description="Internal database application ID (alternative to application_no)")
) -> dict:
    app_no = (application_no or "").strip()
    app_id = application_id
    eff_reason = (reason or "Application rejected per verification criteria").strip()
    deciding_officer = CURRENT_ADMIN_ACTOR.get() or "admin"

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
    portal.write_audit(cur, target_id, "REJECTED", f"officer:{deciding_officer}", eff_reason)
    conn.commit()
    cur.close(); conn.close()

    portal.send_sms(
        target_mobile,
        f"Sewa Setu: Your pension application {target_no} was not approved. Reason: {eff_reason}."
    )

    return {
        "success": True,
        "application_id": target_id,
        "application_no": target_no,
        "status": "REJECTED",
        "decided_by": deciding_officer,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "reason": eff_reason,
        "message": f"Application {target_no} was REJECTED. Reason recorded: '{eff_reason}'. SMS notification sent to applicant."
    }


@admin_mcp.tool(
    name="trigger_deemed_approval",
    description=(
        "Execute the statutory deemed-approval process under the Purvanchal Right to Public Services Act. "
        "Any application pending beyond 15 days is transitioned to DEEMED_APPROVED, logged in the audit trail, "
        "and the applicant is notified."
    )
)
def trigger_deemed_approval() -> dict:
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


def _get_admin_sdk_tools_schema() -> List[Dict[str, Any]]:
    """Helper to retrieve JSON schema for all registered admin tools from the MCP SDK."""
    tools = asyncio.run(admin_mcp.list_tools())
    res = []
    for t in tools:
        schema = t.input_schema if hasattr(t, "input_schema") else {"type": "object", "properties": {}}
        res.append({
            "name": t.name,
            "description": t.description or "",
            "inputSchema": schema,
            "input_schema": schema
        })
    return res


# Cached/dynamic tool definition list for backwards-compatibility
ADMIN_TOOLS = _get_admin_sdk_tools_schema()

def _clean_args(fn, args):
    import inspect
    from pydantic.fields import FieldInfo
    from pydantic_core import PydanticUndefined
    sig = inspect.signature(fn)
    clean = {}
    for name, param in sig.parameters.items():
        if args and name in args:
            clean[name] = args[name]
        elif param.default is not inspect.Parameter.empty:
            d = param.default
            if isinstance(d, FieldInfo):
                clean[name] = "" if d.default is PydanticUndefined else d.default
            else:
                clean[name] = d
    return clean


def _make_admin_handler(tool_fn):
    def _handler(args=None, actor=None):
        token = CURRENT_ADMIN_ACTOR.set(actor)
        try:
            return tool_fn(**_clean_args(tool_fn, args))
        finally:
            CURRENT_ADMIN_ACTOR.reset(token)
    return _handler

ADMIN_TOOL_HANDLERS = {
    "get_admin_dashboard_stats": _make_admin_handler(get_admin_dashboard_stats),
    "list_applications": _make_admin_handler(list_applications),
    "get_application_details": _make_admin_handler(get_application_details),
    "approve_pension_application": _make_admin_handler(approve_pension_application),
    "reject_pension_application": _make_admin_handler(reject_pension_application),
    "trigger_deemed_approval": _make_admin_handler(trigger_deemed_approval),
}



# ---------------------------------------------------------------------------
# Admin JSON-RPC 2.0 Dispatcher (MCP 2024-11-05 Specification)
# ---------------------------------------------------------------------------

def handle_admin_jsonrpc_request(req_data: Any, actor: Optional[str]) -> Optional[Dict[str, Any]]:
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

    # 1. Connection Handshake
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

    # 2. Tool Discovery
    elif method == "tools/list":
        tools = _get_admin_sdk_tools_schema()
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": tools
            }
        }

    # 3. Tool Execution & Input Validation via MCP SDK
    elif method == "tools/call":
        # Strict officer authentication check
        if not actor:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Authentication Required: Access to Block Officer tools requires a valid "
                                "Bearer token in the Authorization header (e.g. 'Authorization: Bearer sewasetu-admin-token-2026') "
                                "or the officer access token."
                            )
                        }
                    ],
                    "isError": True
                }
            }

        tool_name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        elif not isinstance(args, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params: 'arguments' must be an object"}
            }

        cv_token = CURRENT_ADMIN_ACTOR.set(actor)
        try:
            call_res = asyncio.run(admin_mcp.call_tool(tool_name, args))
            content_list = []
            for c in getattr(call_res, "content", []):
                content_list.append({
                    "type": getattr(c, "type", "text"),
                    "text": getattr(c, "text", str(c))
                })

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": content_list,
                    "isError": getattr(call_res, "is_error", False)
                }
            }
        except ToolError as te:
            err_msg = str(te)
            if "Unknown tool:" in err_msg:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Admin tool '{tool_name}' not found."}
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Input validation error: {err_msg}"
                        }
                    ],
                    "isError": True
                }
            }
        except UnexpectedToolError as ute:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Execution error in admin tool '{tool_name}': {str(ute)}"
                        }
                    ],
                    "isError": True
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
                            "text": f"Unexpected error executing admin tool '{tool_name}': {str(e)}"
                        }
                    ],
                    "isError": True
                }
            }
        finally:
            CURRENT_ADMIN_ACTOR.reset(cv_token)

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
    resp.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id"
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

        tools = _get_admin_sdk_tools_schema()
        return jsonify({
            "service": "Sewa Setu Block Officer / Admin MCP Server",
            "version": "1.0.0",
            "status": "ready",
            "protocol": "Model Context Protocol (MCP)",
            "protocol_version": "2024-11-05",
            "sdk": "mcp.server.mcpserver (Official Python MCP SDK)",
            "authenticated": actor is not None,
            "authenticated_as": f"officer:{actor}" if actor else "unauthenticated",
            "transports": {
                "streamable_http": f"{ISSUER}/mcp/admin",
                "sse": f"{ISSUER}/mcp/admin/sse",
                "messages": f"{ISSUER}/mcp/admin/messages"
            },
            "tools_count": len(tools),
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

    session_id = request.headers.get("Mcp-Session-Id") or secrets.token_hex(16)

    if isinstance(body, list):
        responses = [handle_admin_jsonrpc_request(item, actor) for item in body]
        valid_responses = [r for r in responses if r is not None]
        resp = jsonify(valid_responses)
        resp.headers["Mcp-Session-Id"] = session_id
        return resp
    else:
        rpc_resp = handle_admin_jsonrpc_request(body, actor)
        if rpc_resp is None:
            res = Response("", status=204)
            res.headers["Mcp-Session-Id"] = session_id
            return res
        resp = jsonify(rpc_resp)
        resp.headers["Mcp-Session-Id"] = session_id
        return resp


@mcp_admin.route("/mcp/admin/sse", methods=["GET"])
def mcp_admin_sse():
    """MCP SSE Transport connection endpoint for Admin."""
    actor = verify_admin_auth(request)
    return sse_admin_connect(actor)


def sse_admin_connect(actor: Optional[str]):
    session_id = secrets.token_hex(16)
    q: queue.Queue = queue.Queue()

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
            "X-Accel-Buffering": "no",
            "Mcp-Session-Id": session_id
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
