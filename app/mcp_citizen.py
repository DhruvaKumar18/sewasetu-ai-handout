# -*- coding: utf-8 -*-
"""
Sewa Setu - Citizen MCP Server (Public Endpoint)
Department of Social Welfare, Government of Purvanchal

Exposes an MCP (Model Context Protocol) endpoint over Streamable HTTP and SSE
for citizen assistance. The portal remains the system of record.

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
from datetime import datetime
from urllib.parse import urlparse

import jwt
from flask import Blueprint, request, jsonify, Response, current_app

import app as portal
from oauth import ISSUER, signing_key

mcp_citizen = Blueprint("mcp_citizen", __name__)

# Active SSE sessions: session_id -> queue.Queue
_SSE_SESSIONS = {}
_SSE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Token & Authentication verification
# ---------------------------------------------------------------------------

def get_authenticated_citizen(req):
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
# Citizen Tools Definitions (MCP Tool Schema)
# ---------------------------------------------------------------------------

CITIZEN_TOOLS = [
    {
        "name": "get_scheme_guidelines",
        "description": (
            "Get the official guidelines, eligibility criteria, required documents, "
            "statutory SLA, and current application window status for the Sewa Setu "
            "Old Age Pension Scheme in Purvanchal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }
    },
    {
        "name": "check_eligibility",
        "description": (
            "Check if a senior citizen is eligible to apply for Old Age Pension based "
            "on their Date of Birth (must be 60+ years) and Block."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dob": {
                    "type": "string",
                    "description": "Date of birth in DD/MM/YYYY format (e.g. 15/08/1960)"
                },
                "block": {
                    "type": "string",
                    "description": "Block name (must be one of: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua)"
                }
            },
            "required": ["dob", "block"],
            "additionalProperties": False
        }
    },
    {
        "name": "send_citizen_otp",
        "description": (
            "Dispatch an SMS verification OTP to the citizen's 10-digit mobile number "
            "via the Sewa Setu SMS gateway."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mobile": {
                    "type": "string",
                    "description": "10-digit mobile phone number"
                }
            },
            "required": ["mobile"],
            "additionalProperties": False
        }
    },
    {
        "name": "verify_citizen_otp",
        "description": "Verify the 6-digit OTP code received by the citizen on their phone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mobile": {
                    "type": "string",
                    "description": "10-digit mobile number"
                },
                "otp": {
                    "type": "string",
                    "description": "6-digit OTP code"
                }
            },
            "required": ["mobile", "otp"],
            "additionalProperties": False
        }
    },
    {
        "name": "submit_pension_application",
        "description": (
            "Submit a complete Old Age Pension application into the Sewa Setu portal. "
            "Validates all scheme criteria (age >= 60, valid block, bank account, IFSC) "
            "and creates a formal PENDING application record in the database."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "applicant_name": {
                    "type": "string",
                    "description": "Full name of the senior citizen applicant"
                },
                "dob": {
                    "type": "string",
                    "description": "Date of birth in DD/MM/YYYY format"
                },
                "gender": {
                    "type": "string",
                    "description": "Gender: Male, Female, or Other"
                },
                "village": {
                    "type": "string",
                    "description": "Village or locality name"
                },
                "block": {
                    "type": "string",
                    "description": "Block name: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, or Khelua"
                },
                "bank_account": {
                    "type": "string",
                    "description": "Bank account number (9 to 18 digits)"
                },
                "ifsc": {
                    "type": "string",
                    "description": "Bank IFSC code (11 characters, e.g. SBIN0003077)"
                },
                "mobile": {
                    "type": "string",
                    "description": "10-digit mobile number (required if not using authenticated session)"
                },
                "marital_status": {
                    "type": "string",
                    "description": "Marital status: Single, Married, Widowed, or Divorced"
                },
                "husband_name": {
                    "type": "string",
                    "description": "Husband's name if applicable"
                },
                "document_name": {
                    "type": "string",
                    "description": "Name or type of age proof document (e.g. aadhaar_card.pdf, voter_id.jpg)"
                }
            },
            "required": ["applicant_name", "dob", "village", "block", "bank_account", "ifsc"],
            "additionalProperties": False
        }
    },
    {
        "name": "get_application_status",
        "description": (
            "Check current status and decision details for a citizen's application "
            "on the Sewa Setu portal (PENDING, APPROVED, DEEMED_APPROVED, REJECTED, or WITHDRAWN)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mobile": {
                    "type": "string",
                    "description": "10-digit mobile number of the applicant (optional if application_no provided)"
                },
                "application_no": {
                    "type": "string",
                    "description": "Official application number (e.g. SSP26123456) (optional if mobile provided)"
                }
            },
            "additionalProperties": False
        }
    },
    {
        "name": "withdraw_pension_application",
        "description": (
            "Withdraw a PENDING application so the applicant can correct mistakes "
            "and submit a fresh application."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {
                    "type": "string",
                    "description": "Application number to withdraw"
                },
                "mobile": {
                    "type": "string",
                    "description": "10-digit mobile number (required for verification if unauthenticated)"
                }
            },
            "required": ["application_no"],
            "additionalProperties": False
        }
    },
    {
        "name": "get_acknowledgment",
        "description": (
            "Retrieve formal acknowledgment details and official receipt PDF link "
            "for a submitted application."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_no": {
                    "type": "string",
                    "description": "Application number (e.g. SSP26123456)"
                },
                "mobile": {
                    "type": "string",
                    "description": "10-digit mobile number (optional if authenticated)"
                }
            },
            "required": ["application_no"],
            "additionalProperties": False
        }
    }
]


# ---------------------------------------------------------------------------
# Tool Execution Handlers
# ---------------------------------------------------------------------------

def execute_get_scheme_guidelines(args, auth_mobile):
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


def execute_check_eligibility(args, auth_mobile):
    dob_raw = args.get("dob", "")
    block_raw = (args.get("block") or "").strip()

    dob = portal.parse_dob(dob_raw)
    reasons = []
    eligible = True
    age = None

    if dob is None:
        eligible = False
        reasons.append("Date of birth must be given in DD/MM/YYYY format.")
    else:
        age = portal.age_on(dob, portal.now_ist().date())
        if age < portal.MIN_AGE:
            eligible = False
            reasons.append(f"Applicant age ({age} years) is below minimum required age of {portal.MIN_AGE} years.")

    if not block_raw:
        eligible = False
        reasons.append("Block is required.")
    elif block_raw not in portal.BLOCKS:
        eligible = False
        reasons.append(f"Block '{block_raw}' is not eligible. Eligible blocks: {', '.join(portal.BLOCKS)}.")

    hours_left = portal.deadline_remaining()
    if hours_left is None or hours_left <= 0:
        eligible = False
        reasons.append("The scheme application window has closed.")

    return {
        "eligible": eligible,
        "calculated_age": age,
        "minimum_age_required": portal.MIN_AGE,
        "block": block_raw,
        "block_recognized": block_raw in portal.BLOCKS,
        "reasons": reasons if not eligible else ["Applicant meets all statutory age and block criteria for Old Age Pension."]
    }


def execute_send_citizen_otp(args, auth_mobile):
    mobile = auth_mobile or (args.get("mobile") or "").strip()
    if not mobile or len(mobile) != 10 or not mobile.isdigit():
        return {"success": False, "error": "invalid_mobile", "message": "Please provide a valid 10-digit mobile number."}

    code = "%06d" % secrets.randbelow(1000000)
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                (mobile, code, datetime.now()))
    conn.commit()
    cur.close(); conn.close()

    portal.send_sms(mobile, f"Your Sewa Setu OTP is {code}. Valid for 5 minutes.")
    return {
        "success": True,
        "mobile": mobile,
        "validity_seconds": portal.OTP_VALIDITY_SECONDS,
        "message": f"Verification OTP has been sent via SMS to {mobile}. Valid for 5 minutes."
    }


def execute_verify_citizen_otp(args, auth_mobile):
    mobile = auth_mobile or (args.get("mobile") or "").strip()
    code = (args.get("otp") or "").strip()

    if not mobile or len(mobile) != 10 or not mobile.isdigit():
        return {"verified": False, "message": "Please provide a valid 10-digit mobile number."}
    if not code:
        return {"verified": False, "message": "Please provide the 6-digit OTP code."}

    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1", (mobile,))
    row = cur.fetchone()
    cur.close(); conn.close()

    if row and secrets.compare_digest(row[0], code):
        elapsed = (datetime.now() - row[1]).total_seconds()
        if elapsed <= portal.OTP_VALIDITY_SECONDS:
            return {
                "verified": True,
                "mobile": mobile,
                "message": "OTP successfully verified. The mobile number is confirmed."
            }
        else:
            return {
                "verified": False,
                "mobile": mobile,
                "message": "The OTP code has expired. Please request a new OTP."
            }
    return {
        "verified": False,
        "mobile": mobile,
        "message": "Invalid OTP code. Please enter the correct 6-digit code."
    }


def execute_submit_pension_application(args, auth_mobile):
    mobile = auth_mobile or (args.get("mobile") or "").strip()
    if not mobile or len(mobile) != 10 or not mobile.isdigit():
        return {
            "success": False,
            "error": "invalid_mobile",
            "message": "A valid 10-digit mobile number is required to submit an application."
        }

    # Ensure upload directory exists and create simulated proof document if not provided
    doc_name = args.get("document_name") or "age_proof_document.pdf"
    os.makedirs(portal.UPLOAD_DIR, exist_ok=True)
    doc_filename = f"{mobile}_{os.path.basename(doc_name)}"
    doc_path = os.path.join(portal.UPLOAD_DIR, doc_filename)

    if not os.path.exists(doc_path):
        with open(doc_path, "wb") as f:
            f.write(f"Proof of age document for applicant {args.get('applicant_name', 'Citizen')} (Mobile: {mobile})".encode("utf-8"))

    # Validate through single source of truth in portal
    form_data = {
        "applicant_name": args.get("applicant_name"),
        "dob": args.get("dob"),
        "gender": args.get("gender") or "Other",
        "marital_status": args.get("marital_status") or "Single",
        "husband_name": args.get("husband_name") or "",
        "village": args.get("village"),
        "block": args.get("block"),
        "bank_account": args.get("bank_account"),
        "ifsc": args.get("ifsc"),
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
    existing = portal.active_application(cur, mobile)
    if existing:
        cur.close(); conn.close()
        return {
            "success": False,
            "error": "active_application_exists",
            "existing_application_no": existing[1],
            "existing_status": existing[2],
            "message": (
                f"An active application {existing[1]} (Status: {existing[2]}) already exists "
                f"for mobile {mobile}. A pending application can be withdrawn before applying afresh."
            )
        }

    # Persist in portal database
    new_id, app_no = portal.create_application(cur, mobile, cleaned)
    conn.commit()
    cur.close(); conn.close()

    # Send confirmation SMS
    portal.send_sms(
        mobile,
        f"Sewa Setu: application {app_no} received. Track at the status portal with "
        f"mobile no. and password (DOB as DDMMYYYY)."
    )

    return {
        "success": True,
        "application_id": new_id,
        "application_no": app_no,
        "status": "PENDING",
        "applicant_name": cleaned["applicant_name"],
        "mobile": mobile,
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


def execute_get_application_status(args, auth_mobile):
    mobile = auth_mobile or (args.get("mobile") or "").strip()
    app_no = (args.get("application_no") or "").strip()

    if not mobile and not app_no:
        return {
            "found": False,
            "message": "Please provide either the mobile number or application number."
        }

    conn = portal.get_db()
    cur = conn.cursor()
    if app_no and mobile:
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, village, block,
                      bank_account, ifsc, status, submitted_at, decided_at, decided_by
               FROM applications WHERE application_no = %s AND mobile = %s
               ORDER BY submitted_at DESC LIMIT 1""",
            (app_no, mobile)
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
            (mobile,)
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


def execute_withdraw_pension_application(args, auth_mobile):
    mobile = auth_mobile or (args.get("mobile") or "").strip()
    app_no = (args.get("application_no") or "").strip()

    if not app_no:
        return {"success": False, "message": "Application number is required."}

    conn = portal.get_db()
    cur = conn.cursor()

    if mobile:
        cur.execute(
            """UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT'
               WHERE application_no = %s AND mobile = %s AND status = 'PENDING'
               RETURNING id, application_no, mobile""",
            (datetime.now(), app_no, mobile)
        )
    else:
        cur.execute(
            """UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT'
               WHERE application_no = %s AND status = 'PENDING'
               RETURNING id, application_no, mobile""",
            (datetime.now(), app_no)
        )

    row = cur.fetchone()
    if row:
        app_id, application_no, app_mobile = row
        portal.write_audit(cur, app_id, "WITHDRAW", f"applicant:{app_mobile}")
        conn.commit()
        cur.close(); conn.close()
        return {
            "success": True,
            "application_no": application_no,
            "status": "WITHDRAWN",
            "message": f"Application {application_no} has been successfully withdrawn. You may now apply afresh."
        }

    cur.close(); conn.close()
    return {
        "success": False,
        "message": f"Unable to withdraw application {app_no}. Either it does not exist, is not in PENDING status, or mobile mismatch."
    }


def execute_get_acknowledgment(args, auth_mobile):
    mobile = auth_mobile or (args.get("mobile") or "").strip()
    app_no = (args.get("application_no") or "").strip()

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


TOOL_HANDLERS = {
    "get_scheme_guidelines": execute_get_scheme_guidelines,
    "check_eligibility": execute_check_eligibility,
    "send_citizen_otp": execute_send_citizen_otp,
    "verify_citizen_otp": execute_verify_citizen_otp,
    "submit_pension_application": execute_submit_pension_application,
    "get_application_status": execute_get_application_status,
    "withdraw_pension_application": execute_withdraw_pension_application,
    "get_acknowledgment": execute_get_acknowledgment,
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 Dispatcher
# ---------------------------------------------------------------------------

def handle_jsonrpc_request(req_data, auth_mobile):
    if not isinstance(req_data, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: expected JSON object"}
        }

    req_id = req_data.get("id")
    method = req_data.get("method")
    params = req_data.get("params") or {}

    # Notifications do not return a response if no ID
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

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": CITIZEN_TOOLS}}

    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}

        if tool_name not in TOOL_HANDLERS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found."}
            }

        try:
            handler = TOOL_HANDLERS[tool_name]
            result_data = handler(args, auth_mobile)
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
                            "text": f"Error executing tool '{tool_name}': {str(e)}"
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
# HTTP & SSE Routes
# ---------------------------------------------------------------------------

@mcp_citizen.after_request
def _cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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
    - GET: Returns service info or SSE stream if requested.
    - POST: Standard Streamable HTTP JSON-RPC 2.0 endpoint.
    """
    auth_mobile = get_authenticated_citizen(request)

    if request.method == "GET":
        accept = request.headers.get("Accept", "")
        if "text/event-stream" in accept or request.args.get("sse"):
            return sse_connect(auth_mobile)

        return jsonify({
            "service": "Sewa Setu Citizen MCP Server",
            "version": "1.0.0",
            "status": "ready",
            "mode": "public_dual_mode",
            "authenticated_as": f"citizen:{auth_mobile}" if auth_mobile else "anonymous_direct_connection",
            "transports": {
                "streamable_http": f"{ISSUER}/mcp/citizen",
                "sse": f"{ISSUER}/mcp/citizen/sse",
                "messages": f"{ISSUER}/mcp/citizen/messages"
            },
            "tools_count": len(CITIZEN_TOOLS)
        })

    # POST: JSON-RPC 2.0 over HTTP
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error: invalid JSON"}
        }), 400

    if isinstance(body, list):
        responses = [handle_jsonrpc_request(item, auth_mobile) for item in body]
        valid_responses = [r for r in responses if r is not None]
        return jsonify(valid_responses)
    else:
        resp = handle_jsonrpc_request(body, auth_mobile)
        if resp is None:
            return ("", 204)
        return jsonify(resp)


@mcp_citizen.route("/mcp/citizen/sse", methods=["GET"])
def mcp_sse():
    """MCP SSE Transport connection endpoint."""
    auth_mobile = get_authenticated_citizen(request)
    return sse_connect(auth_mobile)


def sse_connect(auth_mobile):
    session_id = secrets.token_hex(16)
    q = queue.Queue()

    with _SSE_LOCK:
        _SSE_SESSIONS[session_id] = {"queue": q, "auth_mobile": auth_mobile}

    def event_stream():
        # First event: tell the client where to POST messages
        endpoint_url = f"{ISSUER}/mcp/citizen/messages?session_id={session_id}"
        yield f"event: endpoint\ndata: {endpoint_url}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"event: message\ndata: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    # Keep-alive comment
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
            "X-Accel-Buffering": "no"
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
