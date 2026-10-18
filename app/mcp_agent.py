# -*- coding: utf-8 -*-
"""
Sewa Setu - Intelligent MCP AI Agent & Natural Language Query Engine
Department of Social Welfare, Government of Purvanchal

Allows citizens and departmental officers to interactively converse with the portal
via natural language. The agent extracts entities, resolves intent, invokes the
appropriate MCP tool from the tool registry, and returns structured tool results
along with polite, citizen-friendly explanations.
"""

import re
import os
import json
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List

# Recognized statutory blocks in Purvanchal
BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]


def extract_mobile(text: str) -> Optional[str]:
    """Extract standard 10-digit Indian mobile number."""
    m = re.search(r'(?:\+91[\-\s]?)?([6-9]\d{9})\b', text)
    return m.group(1) if m else None


def extract_dob_or_age(text: str) -> Dict[str, Any]:
    """Extract date of birth or age from free text."""
    # Pattern 1: DD/MM/YYYY or DD-MM-YYYY
    m = re.search(r'\b([0-3]?\d)[/\-\.]([0-1]?\d)[/\-\.]((?:19|20)\d{2})\b', text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return {"dob": f"{day:02d}/{month:02d}/{year:04d}", "parsed_type": "dob"}

    # Pattern 2: YYYY-MM-DD
    m2 = re.search(r'\b((?:19|20)\d{2})[\-\/\.]([0-1]?\d)[\-\/\.]([0-3]?\d)\b', text)
    if m2:
        year, month, day = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return {"dob": f"{day:02d}/{month:02d}/{year:04d}", "parsed_type": "dob"}

    # Pattern 3: Born in YYYY
    m3 = re.search(r'\b(?:born\s+(?:in|around)\s+)((?:19|20)\d{2})\b', text, re.IGNORECASE)
    if m3:
        year = int(m3.group(1))
        return {"dob": f"01/01/{year:04d}", "parsed_type": "birth_year"}

    # Pattern 4: Explicit age (e.g. "age 67", "67 years old", "am 67")
    m4 = re.search(r'\b(?:age[d]?\s+|am\s+|i\'m\s+)(\d{1,2})\b', text, re.IGNORECASE)
    if not m4:
        m4 = re.search(r'\b(\d{1,2})\s*(?:years|yrs|year)\s*old\b', text, re.IGNORECASE)
    if m4:
        age = int(m4.group(1))
        current_year = datetime.now().year
        birth_year = current_year - age
        return {"dob": f"01/01/{birth_year:04d}", "age": age, "parsed_type": "age"}

    return {}


def extract_application_no(text: str) -> Optional[str]:
    """Extract formal application number (SSP/2026/XXXXXX)."""
    m = re.search(r'(SSP/\d{4}/\d{6})', text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Check bare digits like 000001
    m2 = re.search(r'\b(\d{6})\b', text)
    if m2:
        return f"SSP/2026/{m2.group(1)}"
    return None


def extract_otp(text: str) -> Optional[str]:
    """Extract 6-digit OTP code."""
    m = re.search(r'\b(\d{6})\b', text)
    return m.group(1) if m else None


def extract_block(text: str) -> Optional[str]:
    """Find recognized statutory block if mentioned."""
    lower = text.lower()
    for b in BLOCKS:
        if b.lower() in lower:
            return b
    return None


def process_citizen_query(query: str, session_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Intelligently analyzes a natural language question from a citizen, determines
    the appropriate Citizen MCP tool to invoke, executes it against the live portal,
    and returns a structured response with a conversational explanation.
    """
    from mcp_citizen import TOOL_HANDLERS

    q_clean = (query or "").strip()
    q_lower = q_clean.lower()

    mobile = extract_mobile(q_clean) or (session_data.get("mobile") if session_data else None)
    dob_data = extract_dob_or_age(q_clean)
    app_no = extract_application_no(q_clean)
    block = extract_block(q_clean)
    otp = extract_otp(q_clean)

    # 1. Greetings
    if any(q_lower.startswith(w) for w in ["hi", "hello", "hey", "namaskar", "pranam"]) and len(q_lower.split()) <= 3:
        return {
            "query": q_clean,
            "intent": "general_greeting",
            "tool_called": "get_scheme_guidelines",
            "tool_args": {},
            "tool_result": TOOL_HANDLERS["get_scheme_guidelines"](),
            "answer": (
                "Namaskar! I am your Sewa Setu AI Pension Assistant. "
                "I can verify your eligibility, track your application status, inspect statutory guidelines, "
                "or guide you through submitting a new Old Age Pension application. How can I help you today?"
            ),
            "suggested_actions": [
                "Am I eligible for pension? (Check age)",
                "What documents are required?",
                "Track application status",
                "What is the monthly pension amount?"
            ]
        }

    # 2. Application Status Tracking
    if any(k in q_lower for k in ["status", "track", "check application", "did my application", "where is my application", "progress", "update on application"]):
        if mobile:
            res = TOOL_HANDLERS["get_application_status"]({"mobile": mobile})
            if res.get("found"):
                status = res.get("status", "PENDING")
                app_num = res.get("application_no", "N/A")
                block_name = res.get("block", "N/A")
                sub_date = res.get("submitted_at", "N/A")[:10]
                badge = "🟡 Under Review" if status == "PENDING" else ("🟢 Approved" if "APPROVED" in status else "🔴 " + status)

                return {
                    "query": q_clean,
                    "intent": "get_application_status",
                    "tool_called": "get_application_status",
                    "tool_args": {"mobile": mobile},
                    "tool_result": res,
                    "answer": (
                        f"Found application **{app_num}** associated with mobile **{mobile}**.\n\n"
                        f"• **Status**: {status} ({badge})\n"
                        f"• **Applicant Name**: {res.get('name')}\n"
                        f"• **Block**: {block_name}\n"
                        f"• **Submitted Date**: {sub_date}\n\n"
                        + ("Your application is within the statutory 15-day SLA review window. The block officer will record their decision shortly."
                           if status == "PENDING" else f"Decision recorded: {status}.")
                    ),
                    "suggested_actions": ["Download Acknowledgment PDF", "Check Scheme Guidelines"]
                }
            else:
                return {
                    "query": q_clean,
                    "intent": "get_application_status",
                    "tool_called": "get_application_status",
                    "tool_args": {"mobile": mobile},
                    "tool_result": res,
                    "answer": (
                        f"No active Old Age Pension application was found for mobile number **{mobile}**. "
                        "Would you like to check your eligibility and submit a new application?"
                    ),
                    "suggested_actions": ["Check Eligibility", "View Scheme Guidelines", "Apply Online"]
                }
        elif app_no:
            res = TOOL_HANDLERS["get_acknowledgment"]({"application_no": app_no})
            if res.get("found"):
                return {
                    "query": q_clean,
                    "intent": "get_acknowledgment",
                    "tool_called": "get_acknowledgment",
                    "tool_args": {"application_no": app_no},
                    "tool_result": res,
                    "answer": (
                        f"Found formal record for application **{app_no}**.\n\n"
                        f"• **Applicant**: {res.get('name')}\n"
                        f"• **Status**: {res.get('status')}\n"
                        f"• **Receipt PDF**: [Download Official Receipt]({res.get('download_url')})"
                    ),
                    "suggested_actions": ["Download Acknowledgment PDF"]
                }
            else:
                return {
                    "query": q_clean,
                    "intent": "get_application_status",
                    "tool_called": "get_acknowledgment",
                    "tool_args": {"application_no": app_no},
                    "tool_result": res,
                    "answer": f"Application **{app_no}** could not be found in the registry. Please check the digits and try again.",
                    "suggested_actions": ["Check with Mobile Number", "View Guidelines"]
                }
        else:
            return {
                "query": q_clean,
                "intent": "get_application_status",
                "tool_called": None,
                "tool_args": {},
                "tool_result": None,
                "answer": (
                    "To look up your application status, please provide your **10-digit mobile number** "
                    "(e.g. 9864012345) or your **Application Number** (e.g. SSP/2026/000001)."
                ),
                "suggested_actions": ["Status for 9864012345", "Status for 9864044332"]
            }

    # 3. Eligibility Check
    if any(k in q_lower for k in ["eligib", "qualify", "can i get", "can i apply", "born", "age", "old enough"]):
        if dob_data.get("dob"):
            dob_str = dob_data["dob"]
            res = TOOL_HANDLERS["check_eligibility"]({
                "dob": dob_str,
                "resident": True,
                "block": block or "Borgaon"
            })
            age = res.get("calculated_age") if res.get("calculated_age") is not None else dob_data.get("age", 0)
            eligible = res.get("eligible", False)

            if eligible:
                answer = (
                    f"✅ **You are Eligible!**\n\n"
                    f"Based on your date of birth ({dob_str}), your calculated age is **{age} years**, "
                    f"which satisfies the statutory requirement of **at least 60 years** under the "
                    f"Purvanchal Right to Public Services Act.\n\n"
                    f"• Monthly Pension: ₹1,000 / month\n"
                    f"• Recognized Blocks: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua\n\n"
                    f"You can proceed to apply online or ask me to guide you through the required documents."
                )
            else:
                answer = (
                    f"❌ **Age Threshold Not Met**\n\n"
                    f"Based on your date of birth ({dob_str}), your calculated age is **{age} years**. "
                    f"Under official scheme guidelines, the statutory minimum age is strictly **60 years**.\n\n"
                    f"Therefore, you are currently not eligible for the Old Age Pension. "
                    f"You may apply once you attain 60 years of age."
                )

            return {
                "query": q_clean,
                "intent": "check_eligibility",
                "tool_called": "check_eligibility",
                "tool_args": {"dob": dob_str, "block": block},
                "tool_result": res,
                "answer": answer,
                "suggested_actions": ["What documents are needed?", "Check Scheme Guidelines", "How to apply?"]
            }
        else:
            return {
                "query": q_clean,
                "intent": "check_eligibility",
                "tool_called": "get_scheme_guidelines",
                "tool_args": {},
                "tool_result": TOOL_HANDLERS["get_scheme_guidelines"](),
                "answer": (
                    "To qualify for the **Sewa Setu Old Age Pension Scheme**:\n\n"
                    "1. **Minimum Age**: You must be at least **60 years of age**.\n"
                    "2. **Residency**: You must reside in one of the 6 recognized blocks (Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua).\n"
                    "3. **Income / Need**: Intended for senior citizens without substantial independent income.\n\n"
                    "What is your **date of birth** or **age**? Tell me (e.g. *'Born on 12/04/1959'*), and I will calculate your eligibility instantly."
                ),
                "suggested_actions": ["Born on 12/04/1959", "Born on 20/09/1972", "Age 65 from Sonari"]
            }

    # 4. OTP Dispatch
    if any(k in q_lower for k in ["otp", "verification code", "send code", "verify mobile"]) and ("send" in q_lower or "get" in q_lower or "request" in q_lower):
        if mobile:
            res = TOOL_HANDLERS["send_citizen_otp"]({"mobile": mobile})
            return {
                "query": q_clean,
                "intent": "send_citizen_otp",
                "tool_called": "send_citizen_otp",
                "tool_args": {"mobile": mobile},
                "tool_result": res,
                "answer": (
                    f"✅ **OTP Dispatched!**\n\n"
                    f"A 6-digit verification code has been transmitted to mobile number **{mobile}**.\n\n"
                    f"• The code is valid for 5 minutes (300 seconds).\n"
                    f"• In this demonstration environment, incoming messages are instantly readable in the simulated telecom console at **/__gateway/**."
                ),
                "suggested_actions": ["Open SMS Gateway (/__gateway/)", "Check Application Status"]
            }
        else:
            return {
                "query": q_clean,
                "intent": "send_citizen_otp",
                "tool_called": None,
                "tool_args": {},
                "tool_result": None,
                "answer": "Please provide the 10-digit mobile number you would like the OTP delivered to.",
                "suggested_actions": ["Send OTP to 9864012345"]
            }

    # 5. Acknowledgment / Receipt
    if any(k in q_lower for k in ["acknowledgment", "receipt", "pdf", "proof", "download"]):
        if app_no:
            res = TOOL_HANDLERS["get_acknowledgment"]({"application_no": app_no})
            return {
                "query": q_clean,
                "intent": "get_acknowledgment",
                "tool_called": "get_acknowledgment",
                "tool_args": {"application_no": app_no},
                "tool_result": res,
                "answer": (
                    f"Official acknowledgment for **{app_no}**:\n\n"
                    f"• Status: {res.get('status')}\n"
                    f"• Direct PDF URL: [{res.get('download_url')}]({res.get('download_url')})"
                ),
                "suggested_actions": ["Track Application Status"]
            }

    # 6. Scheme Guidelines, Documents & General Rules (Default)
    res_guidelines = TOOL_HANDLERS["get_scheme_guidelines"]()
    return {
        "query": q_clean,
        "intent": "get_scheme_guidelines",
        "tool_called": "get_scheme_guidelines",
        "tool_args": {},
        "tool_result": res_guidelines,
        "answer": (
            "Here are the statutory details for the **Sewa Setu Old Age Pension Scheme**:\n\n"
            "• **Financial Benefit**: ₹1,000 per month credited directly to the applicant's verified bank account.\n"
            "• **Age Requirement**: Minimum **60 years** (strictly verified via date of birth).\n"
            "• **Statutory SLA**: **15 working days** under the Right to Public Services Act (applications undecided after 15 days qualify for deemed approval).\n"
            "• **Recognized Blocks**: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua.\n"
            "• **Required Documents**: Aadhaar Card, Age Proof, Bank Passbook copy, and Passport Photograph.\n"
            "• **Application Window**: Active until October 31, 2026."
        ),
        "suggested_actions": [
            "Check My Eligibility (Enter DOB)",
            "Track Application Status",
            "What happens after 15-day SLA?"
        ]
    }


def process_admin_query(query: str, actor: Optional[str] = "admin") -> Dict[str, Any]:
    """
    Intelligently analyzes a departmental administrative query, determines the appropriate
    Admin MCP tool to invoke, and returns an analytical executive summary.
    """
    from mcp_admin import ADMIN_TOOL_HANDLERS

    q_lower = (query or "").lower().strip()
    app_no = extract_application_no(query)
    block = extract_block(query)

    # 1. Stats and Overview
    if any(k in q_lower for k in ["stat", "summary", "count", "metric", "overview", "total", "how many"]):
        res = ADMIN_TOOL_HANDLERS["get_admin_dashboard_stats"]({}, actor=actor)
        return {
            "query": query,
            "intent": "get_admin_dashboard_stats",
            "tool_called": "get_admin_dashboard_stats",
            "tool_args": {},
            "tool_result": res,
            "answer": (
                f"📊 **Departmental Executive Summary** (Actor: `{actor}`):\n\n"
                f"• **Total Applications**: {res.get('total_applications'):,}\n"
                f"• **Approved**: {res.get('by_status', {}).get('APPROVED', 0):,}\n"
                f"• **Deemed Approved (SLA)**: {res.get('by_status', {}).get('DEEMED_APPROVED', 0):,}\n"
                f"• **Pending Decision**: {res.get('by_status', {}).get('PENDING', 0):,}\n"
                f"• **Rejected**: {res.get('by_status', {}).get('REJECTED', 0):,}\n"
                f"• **Overdue Beyond 15-day SLA**: **{res.get('overdue_sla_pending', 0)}** applications\n\n"
                f"Pending applications beyond SLA require immediate review or statutory deemed approval processing."
            ),
            "suggested_actions": ["List Pending Applications", "Audit Overdue SLA Breaches", "Deemed Approval Check"]
        }

    # 2. Application Queue Inspection
    if any(k in q_lower for k in ["list", "queue", "pending", "show applications", "review"]):
        args = {"status": "PENDING", "limit": 5}
        if block:
            args["block"] = block
        res = ADMIN_TOOL_HANDLERS["list_applications"](args, actor=actor)
        apps = res.get("applications", [])
        lines = [f"• **{a.get('application_no')}**: {a.get('name')} ({a.get('block')} · {a.get('village')}) — Age {a.get('age')}, Status: `{a.get('status')}`" for a in apps]
        return {
            "query": query,
            "intent": "list_applications",
            "tool_called": "list_applications",
            "tool_args": args,
            "tool_result": res,
            "answer": (
                f"📋 **Application Queue** ({len(apps)} displayed out of {res.get('total_matched')} matching records):\n\n"
                + "\n".join(lines)
            ),
            "suggested_actions": ["Audit SLA Compliance", "View Dashboard Stats"]
        }

    # 3. Application Details
    if app_no:
        res = ADMIN_TOOL_HANDLERS["get_application_details"]({"application_no": app_no}, actor=actor)
        if res.get("found"):
            return {
                "query": query,
                "intent": "get_application_details",
                "tool_called": "get_application_details",
                "tool_args": {"application_no": app_no},
                "tool_result": res,
                "answer": (
                    f"Dossier for **{app_no}**:\n\n"
                    f"• **Applicant**: {res.get('name')}\n"
                    f"• **Age / DOB**: {res.get('age')} years (DOB: {res.get('dob')})\n"
                    f"• **Village & Block**: {res.get('village')}, {res.get('block')}\n"
                    f"• **Bank Details**: A/C `{res.get('bank_account')}` ({res.get('ifsc')})\n"
                    f"• **Status**: `{res.get('status')}` (SLA Overdue: {res.get('is_sla_overdue')})\n"
                    f"• **Submitted**: {res.get('submitted_at')}\n"
                ),
                "suggested_actions": [f"Approve {app_no}", f"Reject {app_no}"]
            }

    # 4. Deemed Approval Check
    if any(k in q_lower for k in ["deemed", "sla", "overdue", "breach", "trigger"]):
        res = ADMIN_TOOL_HANDLERS["trigger_deemed_approval"]({"dry_run": True}, actor=actor)
        return {
            "query": query,
            "intent": "trigger_deemed_approval",
            "tool_called": "trigger_deemed_approval",
            "tool_args": {"dry_run": True},
            "tool_result": res,
            "answer": (
                f"⚖️ **Statutory Deemed Approval Audit (SLA: 15 Days)**:\n\n"
                f"• Eligible Breached Applications: **{res.get('eligible_for_deemed_approval')}**\n"
                f"• Status: {res.get('message')}\n"
                f"• Execution Mode: Dry Run (No database mutations committed)."
            ),
            "suggested_actions": ["View Dashboard Summary", "Inspect Queue"]
        }

    # Default fallback
    res_def = ADMIN_TOOL_HANDLERS["get_admin_dashboard_stats"]({}, actor=actor)
    return {
        "query": query,
        "intent": "get_admin_dashboard_stats",
        "tool_called": "get_admin_dashboard_stats",
        "tool_args": {},
        "tool_result": res_def,
        "answer": (
            "Departmental Assistant online. You can inspect queues, check SLA metrics, "
            "or review specific dossiers. Currently, there are **"
            f"{res_def.get('by_status', {}).get('PENDING', 0):,} pending applications** "
            f"and **{res_def.get('overdue_sla_pending', 0)} overdue SLA breaches**."
        ),
        "suggested_actions": ["Show Dashboard Stats", "List Pending Applications", "Check SLA Breaches"]
    }
