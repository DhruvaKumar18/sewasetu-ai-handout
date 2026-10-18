# -*- coding: utf-8 -*-
"""
Sewa Setu - Citizen MCP Server (Official MCP SDK Implementation)
Department of Social Welfare, Government of Purvanchal

Exposes a Model Context Protocol (MCP) server over Streamable HTTP and SSE
for citizen assistance, powered by the official Python MCP SDK (mcp.server.mcpserver).
The portal remains the system of record.

Strict separation: ONLY citizen/applicant tools are exposed here.
Officer tools (approval, rejection, block queues, SLA reports) are never visible.

Authentication (Dual-Mode):
1. Direct connection: Agents connect directly to /mcp/citizen without requiring
   an interactive browser popup. Tools accept citizen mobile as arguments or via
   X-Citizen-Mobile header.
2. OAuth 2.1: If an 'Authorization: Bearer <token>' header is present, the server
   verifies the RS256 JWT, checks scope 'citizen', and locks operations to the
   authenticated citizen's mobile.
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
from flask import Blueprint, request, jsonify, Response, current_app, render_template
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import Tool, TextContent, CallToolResult

mcp_citizen = Blueprint("mcp_citizen", __name__)

import app as portal
from oauth import ISSUER, signing_key, mint_access_token, SESSION

# Active SSE sessions: session_id -> queue.Queue
_SSE_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SSE_LOCK = threading.Lock()

# ContextVar to pass authenticated citizen mobile safely across tool invocations
CURRENT_AUTH_CITIZEN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "CURRENT_AUTH_CITIZEN", default=None
)

# Initialize official MCP SDK Server instance
citizen_mcp = MCPServer(
    name="sewasetu-citizen-mcp",
    instructions=(
        "You are the Citizen Assistant for Sewa Setu Old Age Pension Scheme, Department "
        "of Social Welfare, Purvanchal. You help senior citizens check eligibility (age 60+, "
        "valid blocks), send and verify mobile OTPs, submit pension applications, track "
        "status, and withdraw pending applications. Never invent rules; always call the "
        "tools provided."
    ),
    version="1.0.0"
)


# ---------------------------------------------------------------------------
# Token & Authentication verification
# ---------------------------------------------------------------------------

def get_authenticated_citizen(req) -> Optional[str]:
    """
    Returns the authenticated mobile number if a valid citizen Bearer token
    is supplied in the Authorization header or ?access_token query parameter.
    Returns None if no token or invalid.
    """
    auth_header = req.headers.get("Authorization", "").strip()
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif req.args.get("access_token"):
        token = req.args.get("access_token").strip()

    if token in ("sewasetu-citizen-token-2026", "citizen-token", "sewasetu-citizen-token"):
        direct_mobile = req.headers.get("X-Citizen-Mobile", "").strip()
        if direct_mobile and direct_mobile.isdigit() and len(direct_mobile) == 10:
            return direct_mobile
        return None

    if not token:
        # Check optional direct connection header
        direct_mobile = req.headers.get("X-Citizen-Mobile", "").strip()
        if direct_mobile and direct_mobile.isdigit() and len(direct_mobile) == 10:
            return direct_mobile
        return None

    try:
        kid, priv = signing_key()
        claims = jwt.decode(
            token,
            priv.public_key(),
            algorithms=["RS256"],
            issuer=ISSUER,
            options={"verify_aud": False}
        )
        sub = claims.get("sub", "")
        role = claims.get("role", "")
        scope = claims.get("scope", "")
        if (role == "citizen" or scope == "citizen") and sub.startswith("citizen:"):
            return sub.split(":", 1)[1]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Citizen Tools Definitions (Registered with official MCP SDK)
# ---------------------------------------------------------------------------

@citizen_mcp.tool(
    name="get_scheme_guidelines",
    description=(
        "Get the official guidelines, statutory eligibility criteria, required documents, "
        "recognized blocks, application window status, and statutory SLA rules for the Sewa Setu "
        "Old Age Pension Scheme (Department of Social Welfare, Purvanchal)."
    )
)
def get_scheme_guidelines() -> dict:
    hours_left = portal.deadline_remaining()
    window_open = hours_left is not None and hours_left > 0
    return {
        "scheme_name": "Old Age Pension Scheme",
        "department": "Department of Social Welfare, Government of Purvanchal",
        "minimum_age": portal.MIN_AGE,
        "eligible_blocks": portal.BLOCKS,
        "statutory_sla_days": portal.SLA_DAYS,
        "application_window_open": window_open,
        "deadline_hours_remaining": hours_left if window_open else 0,
        "rules": [
            "Applicant must be 60 years of age or older on the date of application.",
            "Applicant must reside in one of the 6 recognized blocks: " + ", ".join(portal.BLOCKS) + ".",
            "Valid 9-18 digit bank account number and 11-character IFSC code are required.",
            "An age proof document (Aadhaar, Voter ID, Birth Certificate) is required.",
            "Each mobile number can hold at most ONE active application (PENDING, APPROVED, DEEMED_APPROVED).",
            "A pending application can be withdrawn if details need to be corrected, after which the citizen may apply again.",
            "Statutory SLA: Applications pending for more than 15 days are deemed approved by law."
        ]
    }


@citizen_mcp.tool(
    name="check_eligibility",
    description=(
        "Check whether an applicant satisfies statutory criteria for the Old Age Pension: "
        "minimum age 60+ on application date and residence in one of the 6 recognized blocks "
        "(Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua)."
    )
)
def check_eligibility(
    dob: str = Field(..., description="Applicant date of birth in DD/MM/YYYY format (e.g. 15/08/1954)"),
    block: str = Field(..., description="Applicant's block of residence (e.g. Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua)"),
    annual_income: Optional[float] = Field(0.0, description="Annual household income in INR (optional)")
) -> dict:
    parsed_dob = portal.parse_dob(dob)
    reasons = []
    eligible = True
    age = None

    if parsed_dob is None:
        eligible = False
        reasons.append("Date of birth must be given in DD/MM/YYYY format.")
    else:
        age = portal.age_on(parsed_dob, portal.now_ist().date())
        if age < portal.MIN_AGE:
            eligible = False
            reasons.append(f"Applicant age ({age} years) is below minimum required age of {portal.MIN_AGE} years.")

    block_clean = (block or "").strip()
    if not block_clean:
        eligible = False
        reasons.append("Block is required.")
    elif block_clean not in portal.BLOCKS:
        eligible = False
        reasons.append(f"Block '{block_clean}' is not eligible. Eligible blocks: {', '.join(portal.BLOCKS)}.")

    hours_left = portal.deadline_remaining()
    if hours_left is None or hours_left <= 0:
        eligible = False
        reasons.append("The scheme application window has closed.")

    return {
        "eligible": eligible,
        "calculated_age": age,
        "minimum_age_required": portal.MIN_AGE,
        "block": block_clean,
        "block_recognized": block_clean in portal.BLOCKS,
        "reasons": reasons if not eligible else ["Applicant meets all statutory age and block criteria for Old Age Pension."]
    }


@citizen_mcp.tool(
    name="send_citizen_otp",
    description=(
        "Send a 6-digit one-time password (OTP) via SMS to the applicant's 10-digit mobile "
        "number for verification and authentication. Valid for 5 minutes."
    )
)
def send_citizen_otp(
    mobile: Optional[str] = Field(None, description="10-digit mobile number of applicant. If not provided, uses authenticated session.")
) -> dict:
    eff_mobile = CURRENT_AUTH_CITIZEN.get() or (mobile or "").strip()
    if not eff_mobile or len(eff_mobile) != 10 or not eff_mobile.isdigit():
        return {
            "success": False,
            "error": "invalid_mobile",
            "message": "Please provide a valid 10-digit mobile number."
        }

    code = "%06d" % secrets.randbelow(1000000)
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
        (eff_mobile, code, datetime.now())
    )
    conn.commit()
    cur.close(); conn.close()

    portal.send_sms(eff_mobile, f"Your Sewa Setu OTP is {code}. Valid for 5 minutes.")
    return {
        "success": True,
        "mobile": eff_mobile,
        "validity_seconds": portal.OTP_VALIDITY_SECONDS,
        "message": f"Verification OTP has been sent via SMS to {eff_mobile}. Valid for 5 minutes."
    }


@citizen_mcp.tool(
    name="verify_citizen_otp",
    description=(
        "Verify the 6-digit OTP received via SMS. On successful verification, "
        "authenticates the citizen and issues a scoped Bearer token."
    )
)
def verify_citizen_otp(
    otp: str = Field(..., description="6-digit OTP code received via SMS"),
    mobile: Optional[str] = Field(None, description="10-digit mobile number. If not provided, uses authenticated session.")
) -> dict:
    eff_mobile = CURRENT_AUTH_CITIZEN.get() or (mobile or "").strip()
    code = (otp or "").strip()

    if not eff_mobile or len(eff_mobile) != 10 or not eff_mobile.isdigit():
        return {"verified": False, "message": "Please provide a valid 10-digit mobile number."}
    if not code:
        return {"verified": False, "message": "Please provide the 6-digit OTP code."}

    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1",
        (eff_mobile,)
    )
    row = cur.fetchone()
    cur.close(); conn.close()

    if row and secrets.compare_digest(row[0], code):
        elapsed = (datetime.now() - row[1]).total_seconds()
        if elapsed <= portal.OTP_VALIDITY_SECONDS:
            session_ends = datetime.now() + timedelta(seconds=SESSION["citizen"]["session"])
            access_token, claims = mint_access_token(
                subject=f"citizen:{eff_mobile}",
                role="citizen",
                scope="citizen",
                audience=ISSUER,
                client_id="citizen-mcp-agent",
                session_ends=session_ends
            )
            return {
                "verified": True,
                "authenticated": True,
                "mobile": eff_mobile,
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": claims["exp"] - int(time.time()),
                "session_ends": session_ends.isoformat(timespec="seconds"),
                "message": (
                    f"OTP successfully verified. Citizen {eff_mobile} is now authenticated. "
                    f"Bearer token issued for subsequent authenticated sessions."
                )
            }
        else:
            return {
                "verified": False,
                "mobile": eff_mobile,
                "message": "The OTP code has expired. Please request a new OTP."
            }
    return {
        "verified": False,
        "mobile": eff_mobile,
        "message": "Invalid OTP code. Please enter the correct 6-digit code."
    }


@citizen_mcp.tool(
    name="submit_pension_application",
    description=(
        "Submit an official application for the Old Age Pension scheme. Enforces all statutory "
        "rules: applicant must be 60+, residing in an eligible block, valid bank account and IFSC, "
        "and only ONE active application per mobile. System of record is the portal database."
    )
)
def submit_pension_application(
    applicant_name: str = Field(..., description="Full name of applicant as on age proof document"),
    dob: str = Field(..., description="Date of birth in DD/MM/YYYY format"),
    village: str = Field(..., description="Village or ward of residence"),
    block: str = Field(..., description="Block name: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, or Khelua"),
    bank_account: str = Field(..., description="Bank account number (9 to 18 digits)"),
    ifsc: str = Field(..., description="Bank IFSC code (11 characters, e.g. SBIN0003077)"),
    mobile: Optional[str] = Field(None, description="10-digit mobile number (required if not using authenticated session)"),
    gender: Optional[str] = Field("Other", description="Gender: Male, Female, or Other"),
    marital_status: Optional[str] = Field("Single", description="Marital status: Single, Married, Widowed, or Divorced"),
    husband_name: Optional[str] = Field("", description="Husband's name if applicable"),
    document_name: Optional[str] = Field("age_proof.pdf", description="Name of age proof document (e.g. aadhaar_card.pdf, voter_id.jpg)")
) -> dict:
    eff_mobile = CURRENT_AUTH_CITIZEN.get() or (mobile or "").strip()
    if not eff_mobile or len(eff_mobile) != 10 or not eff_mobile.isdigit():
        return {
            "success": False,
            "error": "invalid_mobile",
            "message": "A valid 10-digit mobile number is required to submit an application."
        }

    # Ensure upload directory exists and create simulated proof document
    os.makedirs(portal.UPLOAD_DIR, exist_ok=True)
    doc_clean_name = document_name or "age_proof_document.pdf"
    doc_filename = f"{eff_mobile}_{os.path.basename(doc_clean_name)}"
    doc_path = os.path.join(portal.UPLOAD_DIR, doc_filename)

    if not os.path.exists(doc_path):
        with open(doc_path, "wb") as f:
            f.write(f"Proof of age document for applicant {applicant_name} (Mobile: {eff_mobile})".encode("utf-8"))

    # Validate through single source of truth in portal
    form_data = {
        "applicant_name": applicant_name,
        "dob": dob,
        "gender": gender or "Other",
        "marital_status": marital_status or "Single",
        "husband_name": husband_name or "",
        "village": village,
        "block": block,
        "bank_account": bank_account,
        "ifsc": ifsc,
    }

    errors, cleaned = portal.validate_application(form_data, doc_path)
    if errors:
        return {
            "success": False,
            "error": "validation_failed",
            "errors": errors,
            "message": "Application validation failed: " + "; ".join(errors)
        }

    conn = portal.get_db()
    cur = conn.cursor()

    # Check for active existing application
    existing = portal.active_application(cur, eff_mobile)
    if existing:
        cur.close(); conn.close()
        return {
            "success": False,
            "error": "active_application_exists",
            "existing_application_no": existing[1],
            "existing_status": existing[2],
            "message": (
                f"An active application {existing[1]} (Status: {existing[2]}) already exists "
                f"for mobile {eff_mobile}. A pending application can be withdrawn before applying afresh."
            )
        }

    # Persist in portal database
    new_id, app_no = portal.create_application(cur, eff_mobile, cleaned)
    conn.commit()
    cur.close(); conn.close()

    # Send confirmation SMS
    portal.send_sms(
        eff_mobile,
        f"Sewa Setu: application {app_no} received. Track at the status portal with "
        f"mobile no. and password (DOB as DDMMYYYY)."
    )

    return {
        "success": True,
        "application_id": new_id,
        "application_no": app_no,
        "status": "PENDING",
        "applicant_name": cleaned["applicant_name"],
        "mobile": eff_mobile,
        "dob": cleaned["dob"].strftime("%d/%m/%Y"),
        "block": cleaned["block"],
        "village": cleaned["village"],
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "sla_days": portal.SLA_DAYS,
        "message": (
            f"Pension application {app_no} successfully submitted for {cleaned['applicant_name']}. "
            f"Status is PENDING. Under the Right to Public Services Act, statutory SLA is 15 days."
        )
    }


@citizen_mcp.tool(
    name="get_application_status",
    description=(
        "Check current status and decision details for a citizen's application on the "
        "Sewa Setu portal (PENDING, APPROVED, DEEMED_APPROVED, REJECTED, or WITHDRAWN), "
        "including SLA elapsed days and audit trail."
    )
)
def get_application_status(
    mobile: Optional[str] = Field(None, description="10-digit mobile number of the applicant (optional if application_no provided)"),
    application_no: Optional[str] = Field(None, description="Official application number (e.g. SSP26123456) (optional if mobile provided)")
) -> dict:
    eff_mobile = CURRENT_AUTH_CITIZEN.get() or (mobile or "").strip()
    app_no = (application_no or "").strip()

    if not eff_mobile and not app_no:
        return {
            "found": False,
            "message": "Please provide either the mobile number or application number."
        }

    conn = portal.get_db()
    cur = conn.cursor()
    if app_no and eff_mobile:
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, village, block,
                      bank_account, ifsc, status, submitted_at, decided_at, decided_by
               FROM applications WHERE application_no = %s AND mobile = %s
               ORDER BY submitted_at DESC LIMIT 1""",
            (app_no, eff_mobile)
        )
    elif app_no:
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, village, block,
                      bank_account, ifsc, status, submitted_at, decided_at, decided_by
               FROM applications WHERE application_no = %s
               ORDER BY submitted_at DESC LIMIT 1""",
            (app_no,)
        )
    else:
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, village, block,
                      bank_account, ifsc, status, submitted_at, decided_at, decided_by
               FROM applications WHERE mobile = %s
               ORDER BY submitted_at DESC LIMIT 1""",
            (eff_mobile,)
        )

    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return {
            "found": False,
            "message": "No application found matching the provided details."
        }

    app_id = row[0]
    cur.execute(
        "SELECT action, actor, note, at FROM audit_log WHERE application_id = %s ORDER BY at ASC",
        (app_id,)
    )
    audit_rows = cur.fetchall()
    cur.close(); conn.close()

    audit_trail = [
        {
            "action": a[0],
            "actor": a[1],
            "note": a[2] or "",
            "timestamp": a[3].isoformat(timespec="seconds") if a[3] else None
        }
        for a in audit_rows
    ]

    submitted_at = row[10]
    days_elapsed = (portal.now_ist() - submitted_at).days if submitted_at else 0

    return {
        "found": True,
        "application_id": row[0],
        "application_no": row[1],
        "applicant_name": row[2],
        "mobile": row[3],
        "dob": row[4].strftime("%d/%m/%Y") if row[4] else None,
        "village": row[5],
        "block": row[6],
        "bank_account_last4": row[7][-4:] if row[7] else "",
        "ifsc": row[8],
        "status": row[9],
        "submitted_at": submitted_at.isoformat(timespec="seconds") if submitted_at else None,
        "decided_at": row[11].isoformat(timespec="seconds") if row[11] else None,
        "decided_by": row[12],
        "days_since_submission": days_elapsed,
        "statutory_sla_days": portal.SLA_DAYS,
        "audit_trail": audit_trail,
        "status_description": (
            "Application is pending review by the Block Development Officer." if row[9] == "PENDING" else
            "Application has been approved for old age pension disbursement." if row[9] in ("APPROVED", "DEEMED_APPROVED") else
            "Application was rejected." if row[9] == "REJECTED" else
            "Application was withdrawn by the applicant."
        )
    }


@citizen_mcp.tool(
    name="withdraw_pension_application",
    description=(
        "Withdraw a PENDING application so the applicant can correct mistakes and submit "
        "a fresh application."
    )
)
def withdraw_pension_application(
    application_no: str = Field(..., description="Application number to withdraw (e.g. SSP26123456)"),
    mobile: Optional[str] = Field(None, description="10-digit mobile number (required for verification if unauthenticated)")
) -> dict:
    eff_mobile = CURRENT_AUTH_CITIZEN.get() or (mobile or "").strip()
    app_no = (application_no or "").strip()

    if not app_no:
        return {"success": False, "message": "Application number is required."}

    if not eff_mobile or len(eff_mobile) != 10 or not eff_mobile.isdigit():
        return {
            "success": False,
            "error": "mobile_required",
            "message": "Applicant 10-digit mobile number is required to verify ownership before withdrawing an application."
        }

    conn = portal.get_db()
    cur = conn.cursor()

    cur.execute(
        """UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT'
           WHERE application_no = %s AND mobile = %s AND status = 'PENDING'
           RETURNING id, application_no, mobile""",
        (datetime.now(), app_no, eff_mobile)
    )

    row = cur.fetchone()
    if row:
        app_id, application_no_val, app_mobile = row
        portal.write_audit(cur, app_id, "WITHDRAW", f"applicant:{app_mobile}")
        conn.commit()
        cur.close(); conn.close()
        return {
            "success": True,
            "application_no": application_no_val,
            "status": "WITHDRAWN",
            "message": f"Application {application_no_val} has been successfully withdrawn. You may now apply afresh."
        }

    cur.close(); conn.close()
    return {
        "success": False,
        "message": f"Unable to withdraw application {app_no}. Either it does not exist, is not in PENDING status, or mobile number does not match."
    }


@citizen_mcp.tool(
    name="get_acknowledgment",
    description=(
        "Retrieve formal acknowledgment details and official receipt PDF link for a submitted application."
    )
)
def get_acknowledgment(
    application_no: str = Field(..., description="Application number (e.g. SSP26123456)"),
    mobile: Optional[str] = Field(None, description="10-digit mobile number (optional if authenticated)")
) -> dict:
    app_no = (application_no or "").strip()
    if not app_no:
        return {"found": False, "message": "Application number is required."}

    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, application_no, applicant_name, mobile, dob, village, block,
                  bank_account, ifsc, submitted_at, status
           FROM applications WHERE application_no = %s""",
        (app_no,)
    )
    row = cur.fetchone()
    cur.close(); conn.close()

    if not row:
        return {"found": False, "message": f"Application {app_no} not found."}

    app_id = row[0]
    return {
        "found": True,
        "application_no": row[1],
        "applicant_name": row[2],
        "mobile": row[3],
        "dob": row[4].strftime("%d/%m/%Y") if row[4] else None,
        "village": row[5],
        "block": row[6],
        "bank_account_masked": ("X" * (len(row[7]) - 4) + row[7][-4:]) if row[7] else "",
        "ifsc": row[8],
        "submitted_at": row[9].isoformat(timespec="seconds") if row[9] else None,
        "status": row[10],
        "acknowledgment_pdf_url": f"{ISSUER}/ack/{app_id}.pdf"
    }


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


def _make_citizen_handler(fn):
    def _handler(args=None, auth_mobile=None):
        token = CURRENT_AUTH_CITIZEN.set(auth_mobile)
        try:
            return fn(**_clean_args(fn, args))
        finally:
            CURRENT_AUTH_CITIZEN.reset(token)
    return _handler


TOOL_HANDLERS = {
    "get_scheme_guidelines": _make_citizen_handler(get_scheme_guidelines),
    "check_eligibility": _make_citizen_handler(check_eligibility),
    "send_citizen_otp": _make_citizen_handler(send_citizen_otp),
    "verify_citizen_otp": _make_citizen_handler(verify_citizen_otp),
    "submit_pension_application": _make_citizen_handler(submit_pension_application),
    "get_application_status": _make_citizen_handler(get_application_status),
    "withdraw_pension_application": _make_citizen_handler(withdraw_pension_application),
    "get_acknowledgment": _make_citizen_handler(get_acknowledgment),
}
CITIZEN_TOOL_HANDLERS = TOOL_HANDLERS


def _get_sdk_tools_schema() -> List[Dict[str, Any]]:
    """Helper to retrieve JSON schema for all registered tools from the MCP SDK."""
    tools = asyncio.run(citizen_mcp.list_tools())
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
CITIZEN_TOOLS = _get_sdk_tools_schema()


# ---------------------------------------------------------------------------
# Protocol-Compliant JSON-RPC 2.0 Dispatcher (MCP 2024-11-05 Specification)
# ---------------------------------------------------------------------------

def handle_jsonrpc_request(req_data: Any, auth_mobile: Optional[str]) -> Optional[Dict[str, Any]]:
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
        client_proto = params.get("protocolVersion", "2024-11-05")
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {
                    "listChanged": False
                }
            },
            "serverInfo": {
                "name": "sewasetu-citizen-mcp",
                "version": "1.0.0"
            },
            "instructions": (
                "You are the Citizen Assistant for Sewa Setu Old Age Pension Scheme, Department "
                "of Social Welfare, Purvanchal. You help senior citizens check eligibility (age 60+, "
                "valid blocks), send and verify mobile OTPs, submit pension applications, track "
                "status, and withdraw pending applications. Never invent rules; always call the "
                "tools provided."
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
        tools = _get_sdk_tools_schema()
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": tools
            }
        }

    # 3. Tool Execution & Input Validation via MCP SDK
    elif method == "tools/call":
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

        # Set ContextVar for duration of execution
        cv_token = CURRENT_AUTH_CITIZEN.set(auth_mobile)
        try:
            call_res = asyncio.run(citizen_mcp.call_tool(tool_name, args))
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
                    "error": {"code": -32601, "message": f"Tool '{tool_name}' not found."}
                }
            # Return protocol-compliant tool error response with validation details
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
                            "text": f"Execution error in tool '{tool_name}': {str(ute)}"
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
                            "text": f"Unexpected error executing '{tool_name}': {str(e)}"
                        }
                    ],
                    "isError": True
                }
            }
        finally:
            CURRENT_AUTH_CITIZEN.reset(cv_token)

    else:
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not implemented."}
        }


# ---------------------------------------------------------------------------
# HTTP & SSE Transports
# ---------------------------------------------------------------------------

@mcp_citizen.after_request
def _cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id"
    return resp


@mcp_citizen.route("/mcp/citizen", methods=["OPTIONS"])
@mcp_citizen.route("/mcp/citizen/sse", methods=["OPTIONS"])
@mcp_citizen.route("/mcp/citizen/messages", methods=["OPTIONS"])
def mcp_options():
    return ("", 204)


@mcp_citizen.route("/.well-known/oauth-protected-resource", methods=["GET"])
@mcp_citizen.route("/mcp/citizen/.well-known/oauth-protected-resource", methods=["GET"])
def oauth_protected_resource():
    """RFC 9728: OAuth 2.0 Protected Resource Metadata."""
    return jsonify({
        "resource": ISSUER + "/mcp/citizen",
        "authorization_servers": [ISSUER],
        "scopes_supported": ["citizen"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": ISSUER + "/about"
    })


@mcp_citizen.route("/mcp/citizen", methods=["GET", "POST"])
def mcp_streamable_http():
    """
    Public Streamable HTTP endpoint for Citizen MCP.
    - GET: Returns service info or initiates SSE stream if requested.
    - POST: Streamable HTTP JSON-RPC 2.0 endpoint (single or batch).
    """
    auth_mobile = get_authenticated_citizen(request)

    # If client asks for event stream on GET, delegate to SSE
    accept = request.headers.get("Accept", "")
    if request.method == "GET":
        if "text/event-stream" in accept or request.args.get("sse"):
            return sse_connect(auth_mobile)

        tools = _get_sdk_tools_schema()

        # If accessed by a web browser, render the rich Interactive MCP Explorer UI
        if ("text/html" in accept or "application/xhtml+xml" in accept) and not request.args.get("format") == "json" and not request.args.get("json"):
            return render_template(
                "mcp_citizen_explorer.html",
                tools=tools,
                issuer=ISSUER,
                auth_mobile=auth_mobile,
                tools_count=len(tools)
            )

        return jsonify({
            "service": "Sewa Setu Citizen MCP Server",
            "protocol": "Model Context Protocol (MCP)",
            "protocol_version": "2024-11-05",
            "sdk": "mcp.server.mcpserver (Official Python MCP SDK)",
            "transports": {
                "streamable_http": f"{ISSUER}/mcp/citizen",
                "sse": f"{ISSUER}/mcp/citizen/sse",
                "messages": f"{ISSUER}/mcp/citizen/messages"
            },
            "tools_count": len(tools),
            "tools": [{"name": t["name"], "description": t["description"]} for t in tools],
            "authenticated": auth_mobile is not None,
            "authenticated_mobile": auth_mobile
        })

    # POST: JSON-RPC 2.0 over Streamable HTTP
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error: invalid JSON"}
        }), 400

    # Ensure a session id is assigned/maintained
    session_id = request.headers.get("Mcp-Session-Id") or secrets.token_hex(16)

    if isinstance(body, list):
        responses = [handle_jsonrpc_request(item, auth_mobile) for item in body]
        valid_responses = [r for r in responses if r is not None]
        resp = jsonify(valid_responses)
        resp.headers["Mcp-Session-Id"] = session_id
        return resp
    else:
        rpc_resp = handle_jsonrpc_request(body, auth_mobile)
        if rpc_resp is None:
            res = Response("", status=204)
            res.headers["Mcp-Session-Id"] = session_id
            return res
        resp = jsonify(rpc_resp)
        resp.headers["Mcp-Session-Id"] = session_id
        return resp


@mcp_citizen.route("/mcp/citizen/sse", methods=["GET"])
def mcp_sse():
    """MCP SSE Transport connection endpoint."""
    auth_mobile = get_authenticated_citizen(request)
    return sse_connect(auth_mobile)


def sse_connect(auth_mobile: Optional[str]):
    session_id = secrets.token_hex(16)
    q: queue.Queue = queue.Queue()

    with _SSE_LOCK:
        _SSE_SESSIONS[session_id] = {"queue": q, "auth_mobile": auth_mobile}

    def event_stream():
        # First event: tell the client where to POST messages (MCP SSE specification)
        endpoint_url = f"{ISSUER}/mcp/citizen/messages?session_id={session_id}"
        yield f"event: endpoint\ndata: {endpoint_url}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"event: message\ndata: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _SSE_LOCK:
                _SSE_SESSIONS.pop(session_id, None)

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


@mcp_citizen.route("/mcp/citizen/messages", methods=["POST"])
def mcp_messages():
    """
    Message handler for SSE sessions or direct POST.
    Dispatches JSON-RPC and sends response both over SSE stream and in HTTP body.
    """
    session_id = request.args.get("session_id", "").strip()
    session_data = None
    with _SSE_LOCK:
        session_data = _SSE_SESSIONS.get(session_id)

    auth_mobile = session_data["auth_mobile"] if session_data else get_authenticated_citizen(request)

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"}
        }), 400

    if isinstance(body, list):
        responses = [handle_jsonrpc_request(item, auth_mobile) for item in body]
        valid_responses = [r for r in responses if r is not None]
        if session_data:
            for r in valid_responses:
                session_data["queue"].put(r)
        return jsonify(valid_responses)
    else:
        resp = handle_jsonrpc_request(body, auth_mobile)
        if resp is not None and session_data:
            session_data["queue"].put(resp)
        if resp is None:
            return ("", 204)
        return jsonify(resp)
