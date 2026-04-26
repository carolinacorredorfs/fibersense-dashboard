#!/usr/bin/env python3
"""
Dashboard generator — Carolina Corredor
=============================================
Consulta Jira en tiempo real y genera el HTML del dashboard.

SETUP (una sola vez):
    pip install requests python-dotenv

CONFIGURACIÓN:
    Crea un archivo .env en la misma carpeta con:
        JIRA_BASE_URL=https://fibersense.atlassian.net
        JIRA_EMAIL=tu-email@fibersense.com
        JIRA_API_TOKEN=tu-api-token   ← genera en: https://id.atlassian.com/manage-profile/security/api-tokens

USO:
    python generate_dashboard.py
    → genera: dashboard_carolina.html (ábrelo en cualquier browser)

    python generate_dashboard.py --open
    → genera y abre el browser automáticamente
"""

import os
import sys
import json
import math
import base64
import argparse
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌  Falta la librería 'requests'. Instálala con:\n    pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env manual también funciona sin python-dotenv

# ──────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────
JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL",  "")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL",     "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
ASSIGNEE_ID    = ""   # overridden at runtime from PEOPLE dict

# ── People registry ───────────────────────────────────
PEOPLE = {
    "carolina": {
        "id":       os.getenv("CAROLINA_ACCOUNT_ID", ""),
        "name":     "Carolina Corredor",
        "initials": "CC",
        "color":    "#2DD4BF",
        "file":     "dashboard_carolina.html",
    },
    "rachel": {
        "id":       os.getenv("RACHEL_ACCOUNT_ID", ""),
        "name":     "Rachel Hatch-Ibarra",
        "initials": "RH",
        "color":    "#2DD4BF",
        "file":     "dashboard_rachel.html",
    },
}
CHECKLIST_FIELD = "customfield_10490"
HELP_FIELD      = "customfield_10727"   # Help          # Hardware Checklist
CHECKLIST_TOTAL = 9                            # boxes totales del checklist

OUTPUT_FILE = Path(__file__).parent / "dashboard_carolina.html"


# ──────────────────────────────────────────────
# JIRA API
# ──────────────────────────────────────────────
def auth_headers():
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def jira_search(jql, fields, max_results=100):
    # Try new endpoint first, fall back to legacy
    for endpoint in ("/rest/api/3/search/jql", "/rest/api/3/search"):
        url = f"{JIRA_BASE_URL}{endpoint}"
        payload = {"jql": jql, "fields": fields, "maxResults": max_results}
        r = requests.post(url, headers=auth_headers(), json=payload, timeout=30)
        if r.status_code == 404:
            continue  # try next endpoint
        r.raise_for_status()
        return r.json().get("issues", [])
    raise Exception("No se pudo conectar con la API de Jira. Verifica JIRA_BASE_URL.")


# ──────────────────────────────────────────────
# LÓGICA DE PORCENTAJE
# ──────────────────────────────────────────────
DECOMMISSION_FLOW = {
    "Raised": 1, "IN-PROGRESS": 2, "Shipped": 3,
    "Received": 3, "Completed": 4, "Done": 4, "Operational": 4,
}
NODEPLOYMENT_FLOW = {
    "To Do": 1, "Open": 1, "IN-PROGRESS": 2, "In Progress": 2,
    "Done": 3, "Completed": 3,
}
STATUS_FALLBACK = {
    "Shipping": 70, "IN-PROGRESS": 40, "In Progress": 40,
    "Procurement": 20, "Design": 10, "On Hold": 0,
    "Staging": 60, "Received": 80, "Installed": 88, "Configured": 100,
}

def pct_checklist(issue):
    checked = issue["fields"].get(CHECKLIST_FIELD) or []
    if not checked:
        return None
    return round(len(checked) / CHECKLIST_TOTAL * 100)

def pct_decommission(status_name):
    step = DECOMMISSION_FLOW.get(status_name, 1)
    return round(step / 4 * 100)

def pct_non_deploy(status_name):
    step = NODEPLOYMENT_FLOW.get(status_name, 1)
    return round(step / 3 * 100)

def pct_from_next_description(issue):
    """Extract a percentage integer from customfield_10627 (Next Description).
    Expects text like '90%', '75 %', '50'. Returns int or None."""
    import re
    field = issue["fields"].get("customfield_10627")
    if not field:
        return None
    # Walk ADF content tree to collect all text
    def extract_text(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                return node.get("text", "")
            parts = []
            for child in node.get("content", []):
                parts.append(extract_text(child))
            return " ".join(p for p in parts if p)
        return ""
    raw = extract_text(field).strip()
    match = re.search(r"(\d{1,3})\s*%?", raw)
    if match:
        val = int(match.group(1))
        if 0 <= val <= 100:
            return val
    return None

def get_pct(issue, category):
    status = issue["fields"]["status"]["name"]
    if category in ("deployment", "cisco"):
        p = pct_checklist(issue)
        return p if p is not None else STATUS_FALLBACK.get(status, 0)
    elif category == "decommission":
        return pct_decommission(status)
    else:  # non-deployment — use Next Description field first, fallback to status flow
        p = pct_from_next_description(issue)
        return p if p is not None else pct_non_deploy(status)

def gate_symbol(pct, status):
    if status == "On Hold":
        return ("⏸", "gate-blocked")
    if pct >= 30:
        return ("✓", "gate-green")
    return ("!", "gate-risk")


# ──────────────────────────────────────────────
# CONSULTAS JIRA
# ──────────────────────────────────────────────
def fetch_active_issues():
    fields = ["summary", "status", "priority", "duedate", "issuetype",
              "parent", CHECKLIST_FIELD, "customfield_10627", HELP_FIELD]

    # Deployment / Cisco child issues
    jql_children = (
        f'assignee = "{ASSIGNEE_ID}" AND issueType not in ("Node Deployment") '
        f'AND parent is not EMPTY AND duedate <= "2026-05-10" '
        f'AND statusCategory != Done AND project = SDO ORDER BY duedate ASC'
    )
    # Non-deployment tasks
    jql_tasks = (
        f'assignee = "{ASSIGNEE_ID}" AND issueType in ("Decommission","Task") '
        f'AND parent is EMPTY AND statusCategory != Done AND project = SDO '
        f'AND (duedate <= "2026-05-10" OR duedate < now()) ORDER BY duedate ASC'
    )
    return (
        jira_search(jql_children, fields),
        jira_search(jql_tasks,    fields),
    )


def fetch_monthly_completions():
    # Semanas del mes en curso (aprox. últimas 5 semanas)
    today = datetime.now(timezone.utc)
    # Primer día de la ventana mensual: inicio del mes anterior a hoy o hace 35 días
    start = (today - timedelta(days=35)).strftime("%Y-%m-%d")
    jql = (
        f'assignee = "{ASSIGNEE_ID}" AND project = SDO '
        f'AND statusCategory = Done AND resolutiondate >= "{start}" '
        f'ORDER BY resolutiondate ASC'
    )
    fields = ["summary", "resolutiondate"]
    issues = jira_search(jql, fields, max_results=200)

    # Agrupar en 5 semanas (Thu→Wed) desde hace 5 semanas
    # Week 1 = más antigua, semana 5 = actual
    weeks = [[] for _ in range(5)]
    now = datetime.now(timezone.utc)
    # Encontrar el jueves más reciente como inicio de semana actual
    days_since_thu = (now.weekday() - 3) % 7
    week5_start = now - timedelta(days=days_since_thu)
    week5_start = week5_start.replace(hour=0, minute=0, second=0, microsecond=0)

    for issue in issues:
        rd = issue["fields"].get("resolutiondate")
        if not rd:
            continue
        # parse ISO date
        try:
            dt = datetime.fromisoformat(rd.replace("Z", "+00:00"))
        except Exception:
            continue
        delta_days = (week5_start - dt).days
        if delta_days < 0:
            weeks[4].append(issue)  # current week
        elif delta_days < 7:
            weeks[4].append(issue)
        elif delta_days < 14:
            weeks[3].append(issue)
        elif delta_days < 21:
            weeks[2].append(issue)
        elif delta_days < 28:
            weeks[1].append(issue)
        elif delta_days < 35:
            weeks[0].append(issue)

    counts = [len(w) for w in weeks]
    total  = sum(counts)
    return counts, total, week5_start


def categorize(child_issues, task_issues):
    """Assign each issue to deployment/cisco/decommission/non-deployment."""
    NETWORK_UPGRADE_PARENTS = {"SDO-1901"}
    result = {
        "deployment":    [],
        "cisco":         [],
        "decommission":  [],
        "non_deploy":    [],
    }
    for issue in child_issues:
        parent_key = issue["fields"].get("parent", {}).get("key", "")
        if parent_key in NETWORK_UPGRADE_PARENTS:
            result["cisco"].append(issue)
        else:
            result["deployment"].append(issue)
    for issue in task_issues:
        itype = issue["fields"]["issuetype"]["name"]
        if itype == "Decommission":
            result["decommission"].append(issue)
        else:
            result["non_deploy"].append(issue)
    return result


# ──────────────────────────────────────────────
# HTML GENERATION
# ──────────────────────────────────────────────
HTML_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap');
:root{
  --bg:#0A1628;
  --surface:#0D2233;
  --surface2:#0D3B4F;
  --surface3:#0F4560;
  --border:rgba(45,212,191,.15);
  --border2:rgba(45,212,191,.3);
  --c-teal:#2DD4BF;
  --c-pink:#F472B6;
  --c-amber:#FBBF24;
  --c-red:#F87171;
  --c-gray:#64748B;
  --text1:#FFFFFF;
  --text2:#94A3B8;
  --text3:#64748B;
  --bar-track:rgba(45,212,191,.12);
  --bar-hold:rgba(244,114,182,.12);
  --bar-early:rgba(251,191,36,.12);
  --r:10px;--r-sm:6px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text1);min-height:100vh;padding:0 0 64px}
.brand-header{background:#0A1628;padding:18px 28px 16px;display:flex;align-items:center;gap:14px;border-bottom:1px solid rgba(168,237,234,.18)}
.brand-left{display:flex;align-items:center;gap:14px}
.brand-header svg{width:44px;height:44px;flex-shrink:0}
.brand-tagline{font-size:10px;color:var(--teal);letter-spacing:.08em;text-transform:uppercase;margin-top:2px;font-weight:500}
.brand-date{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3)}
.page-inner{padding:28px 36px 36px}
.page-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:28px}
.page-header h1{font-size:20px;font-weight:700;letter-spacing:.08em;line-height:1.2;font-family:'Goldman',sans-serif;text-transform:uppercase;color:#FFFFFF}
.page-header p{font-size:12px;color:var(--text2);margin-top:5px}
.person-tag{display:flex;align-items:center;gap:8px;background:var(--surface2);
  border:1px solid var(--border);border-radius:99px;padding:6px 16px 6px 8px;
  font-size:12px;color:var(--text2)}
.av{width:26px;height:26px;border-radius:50%;background:rgba(45,212,191,.2);color:var(--teal);
  font-size:10px;font-weight:600;display:flex;align-items:center;justify-content:center;
  border:1px solid var(--border2)}
.monthly{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:20px 24px;margin-bottom:20px}
.monthly-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.monthly-title{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--text3)}
.monthly-badge{font-family:'DM Mono',monospace;font-size:11px;font-weight:500;color:#0A1628;background:#A8EDEA;padding:3px 12px;border-radius:99px;border:1px solid #A8EDEA}
.weeks{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.week{background:var(--surface2);border:1px solid rgba(255,255,255,.05);
  border-radius:var(--r-sm);padding:12px;text-align:center}
.week.current{border-color:#A8EDEA;background:rgba(168,237,234,.07)}
.week-label{font-size:9px;color:var(--text3);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.week-dates{font-size:8px;color:var(--text3);margin:2px 0 8px}
.week-num{font-family:'DM Mono',monospace;font-size:28px;font-weight:400;color:var(--text1);line-height:1}
.week.current .week-num{color:#A8EDEA}
.week-bar{height:2px;background:rgba(255,255,255,.08);border-radius:2px;margin-top:8px;overflow:hidden}
.week-fill{height:100%;border-radius:2px;background:#A8EDEA}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:16px}
.chip{font-size:10px;font-weight:500;padding:3px 10px;border-radius:99px;border:1px solid}
.chip-total{background:var(--surface2);color:var(--text1);border-color:rgba(255,255,255,.12)}
.chip-ip{background:rgba(45,212,191,.1);color:var(--teal);border-color:rgba(45,212,191,.3)}
.chip-hold{background:rgba(244,63,94,.1);color:#FB7185;border-color:rgba(244,63,94,.3)}
.chip-pend{background:rgba(245,158,11,.08);color:#FCD34D;border-color:rgba(245,158,11,.25)}
.gantt-wrap{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
table{width:100%;border-collapse:collapse;table-layout:fixed}
col.lc{width:28%}col.sc{width:10%}col.pc{width:54%}col.gs{width:8%}
thead th{font-family:'DM Mono',monospace;font-size:9px;font-weight:500;text-transform:uppercase;
  letter-spacing:.07em;color:var(--text3);text-align:center;padding:10px 4px;
  background:var(--surface2);border-bottom:1px solid var(--border)}
thead th.lh{text-align:left;padding-left:16px}
thead th.today{color:#A8EDEA;background:rgba(168,237,234,.07)}
td{padding:2px 3px;vertical-align:middle;border-bottom:1px solid rgba(255,255,255,.04)}
td.lc{padding:4px 8px}
tr:last-child td{border-bottom:none}
.cat-row td{background:rgba(45,212,191,.05);padding:6px 16px;
  border-bottom:1px solid var(--border);border-top:1px solid var(--border)}
.cat-label{font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.08em}
.task-label{display:flex;align-items:center;gap:8px;min-height:34px}
.status-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.task-name{font-size:11px;color:var(--text1);line-height:1.3;font-weight:400}
.task-sub{font-size:9px;color:var(--text3);margin-top:1px}
td.bar-cell{padding:3px 5px;vertical-align:middle}
.bar-wrap{position:relative;width:100%}
.bar{height:22px;border-radius:11px;background:var(--bar-track);position:relative;overflow:hidden;width:100%}
.bar-fill{height:100%;border-radius:11px;position:absolute;top:0;left:0}
.bar-hold{background:var(--bar-hold)}
.bar-early{background:var(--bar-early)}
.bar-fill{height:100%;border-radius:11px;position:absolute;top:0;left:0}
.fill-green{background:#2DD4BF}
.fill-gray{background:#FBBF24}
.pct-label-ext{position:absolute;top:50%;transform:translateY(-50%);font-family:'DM Mono',monospace;font-size:9px;font-weight:500;color:#FFFFFF;white-space:nowrap;padding-left:4px}
td.gate-cell{padding:2px 4px;text-align:center;vertical-align:middle}
.gate{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;margin:auto;font-size:11px;font-weight:700;line-height:1;flex-shrink:0}
.gate-green{background:#2DD4BF;color:#0A1628}
.gate-risk{background:#FBBF24;color:#0A1628}
.gate-blocked{background:#F87171;color:#fff}
.gate-gray{background:#64748B;color:#fff}
.gate-amber{background:#F472B6;color:#0A1628}
.footer{margin-top:28px;padding:0 36px;font-size:10px;color:var(--text3);
  display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px}
</style>
"""


def week_label_dates(week_index, week5_start):
    """Return (label, date_range_str) for a given week index (0=oldest, 4=current)."""
    delta = (4 - week_index) * 7
    w_end   = week5_start - timedelta(days=delta - 6)
    w_start = week5_start - timedelta(days=delta)
    labels = ["Week 1", "Week 2", "Week 3", "Week 4", "Week 5 ◀"]
    MONTHS_ES = ["","Jan","Feb","Tue","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    ds = f"{w_start.day} {MONTHS_ES[w_start.month]}"
    de = f"{w_end.day} {MONTHS_ES[w_end.month]}"
    return labels[week_index], f"{ds} – {de}"


def render_week_card(idx, count, max_count, week5_start, is_current):
    label, dates = week_label_dates(idx, week5_start)
    bar_pct = round(count / max_count * 100) if max_count else 0
    cur_cls = " current" if is_current else ""
    return f"""
    <div class="week{cur_cls}">
      <div class="week-label">{label}</div>
      <div class="week-dates">{dates}</div>
      <div class="week-num">{count}</div>
      <div class="week-bar"><div class="week-fill" style="width:{bar_pct}%"></div></div>
    </div>"""


def dot_color(category, status, pct=0):
    if status == "On Hold":
        return "#F472B6"   # pink
    if pct >= 30:
        return "#2DD4BF"   # teal — in progress / advancing
    return "#FBBF24"       # amber — early stage / low progress   # pink — on hold
    if status in ("Design", "Procurement", "Staging"):
        return "#FBBF24"   # amber — early stage
    return "#2DD4BF"       # teal — in progress / shipping



def state_pill(status):
    """Render a colored pill for the Jira status."""
    colors = {
        "Shipping":    ("#2DD4BF", "#0A1628"),
        "IN-PROGRESS": ("#2DD4BF", "#0A1628"),
        "In Progress": ("#2DD4BF", "#0A1628"),
        "In-Progress": ("#2DD4BF", "#0A1628"),
        "Design":      ("#FBBF24", "#0A1628"),
        "Procurement": ("#FBBF24", "#0A1628"),
        "Staging":     ("#FBBF24", "#0A1628"),
        "On Hold":     ("#F472B6", "#0A1628"),
        "Shipped":     ("#2DD4BF", "#0A1628"),
        "Done":        ("#64748B", "#FFFFFF"),
        "Completed":   ("#64748B", "#FFFFFF"),
        "Received":    ("#2DD4BF", "#0A1628"),
    }
    bg, fg = colors.get(status, ("#64748B", "#FFFFFF"))
    style = f'background:{bg};color:{fg};font-size:9px;font-weight:600;padding:2px 8px;border-radius:99px;white-space:nowrap;letter-spacing:.04em;text-transform:uppercase'
    return f'<span style="{style}">{status}</span>'


def gate_label(sym, gate_cls):
    labels = {
        "gate-green":   "On Track",
        "gate-risk":    "At Risk",
        "gate-blocked": "Blocked",
        "gate-amber":   "On Hold",
    }
    colors = {
        "gate-green":   "#2DD4BF",
        "gate-risk":    "#FBBF24",
        "gate-blocked": "#F87171",
        "gate-amber":   "#F472B6",
    }
    lbl   = labels.get(gate_cls, "")
    color = colors.get(gate_cls, "#64748B")
    lbl_style = f"font-size:8px;color:{color};letter-spacing:.06em;text-transform:uppercase;white-space:nowrap;margin-top:2px;font-weight:600"
    return (f'<div style="display:flex;flex-direction:column;align-items:center;gap:1px">'
            f'<div class="gate {gate_cls}">{sym}</div>'
            f'<span style="{lbl_style}">{lbl}</span></div>')



def render_bar(pct, status):
    if status == "On Hold":
        return '<div class="bar-wrap"><div class="bar bar-hold"></div><span class="pct-label-ext" style="left:8px;color:#F472B6">On Hold</span></div>'
    fill_cls = "fill-green" if pct >= 30 else "fill-gray"
    bar_cls  = "bar" if pct >= 30 else "bar bar-early"
    offset   = min(pct + 1, 93)
    pct_html = f'<span class="pct-label-ext" style="left:{offset}%">{pct}%</span>'
    return f'<div class="bar-wrap"><div class="{bar_cls}"><div class="bar-fill {fill_cls}" style="width:{pct}%"></div></div>{pct_html}</div>'


def render_issue_row(issue, category):
    f        = issue["fields"]
    key      = issue["key"]
    name     = f["summary"]
    status   = f["status"]["name"]
    pct      = get_pct(issue, category)
    sym, gate_cls = gate_symbol(pct, status)
    color    = dot_color(category, status, pct)
    display_name = name if len(name) <= 55 else name[:53] + "…"

    return f"""
  <tr>
    <td class="lc">
      <div style="display:flex;align-items:center;gap:7px;min-height:36px">
        <span style="width:7px;height:7px;border-radius:50%;background:{color};flex-shrink:0"></span>
        <div>
          <div style="font-size:11px;color:var(--text1);line-height:1.3">{display_name}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:1px">{key}</div>
        </div>
      </div>
    </td>
    <td style="text-align:center;padding:3px 6px;vertical-align:middle">{state_pill(status)}</td>
    <td style="padding:3px 8px;vertical-align:middle">{render_bar(pct, status)}</td>
    <td class="gate-cell">{gate_label(sym, gate_cls)}</td>
  </tr>"""


def get_week_header(week5_start):
    """Return Thu–Thu workday column headers: Thu Fri Mon Tue Wed Thu (6 cols)."""
    DAYS_ES = ["Mon","Tue","Wed","Thu","Fri"]
    today = datetime.now(timezone.utc).date()
    # Collect workdays: iterate up to 14 calendar days to find exactly 6 workdays
    days = []
    i = 0
    while len(days) < 6:
        d = week5_start + timedelta(days=i)
        if d.weekday() not in (5, 6):  # skip Sat=5, Sun=6
            days.append(d)
        i += 1
    headers = ""
    for d in days:
        lbl = f"{DAYS_ES[d.weekday()]} {d.day}"
        cls = ' class="today"' if d.date() == today else ""
        headers += f'<th{cls}>{lbl}</th>\n      '
    return headers



def get_help_text(issue):
    """Extract plain text from customfield_10727 (Help). Returns str or None."""
    val = issue["fields"].get(HELP_FIELD)
    if not val:
        return None
    if isinstance(val, str):
        return val.strip() or None
    # ADF document
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                return node.get("text", "")
            return " ".join(walk(c) for c in node.get("content", []))
        return ""
    text = walk(val).strip()
    return text or None


def render_support_section(all_issues_flat):
    """Render the Support Required section — only if any issue has Help text."""
    rows = []
    for issue in all_issues_flat:
        help_text = get_help_text(issue)
        if not help_text:
            continue
        key    = issue["key"]
        name   = issue["fields"]["summary"]
        status = issue["fields"]["status"]["name"]
        f      = issue["fields"]
        pct    = 0  # display only, no need to recalc
        name_s = name if len(name) <= 44 else name[:42] + "…"
        rows.append((key, name_s, status, help_text))

    if not rows:
        return ""

    rows_html = ""
    for key, name, status, help_text in rows:
        # gate color based on status
        dot = "#F472B6" if status == "On Hold" else "#FBBF24"
        rows_html += f"""
    <tr>
      <td style="padding:10px 16px;vertical-align:top;border-bottom:1px solid var(--border)">
        <div style="font-size:11px;color:#FFFFFF;font-weight:500;line-height:1.3">{name}</div>
        <div style="font-size:10px;color:#64748B;margin-top:2px">{key}</div>
      </td>
      <td style="padding:10px 16px;vertical-align:top;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:flex-start;gap:8px">
          <span style="width:6px;height:6px;border-radius:50%;background:{dot};flex-shrink:0;margin-top:4px"></span>
          <span style="font-size:11px;color:#94A3B8;line-height:1.6">{help_text}</span>
        </div>
      </td>
    </tr>"""

    n = len(rows)
    return f"""
<div class="gantt-wrap" style="margin-top:16px">
  <div style="display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface2)">
    <span style="font-size:13px">🙋</span>
    <span style="font-family:'Goldman',sans-serif;font-size:11px;font-weight:400;letter-spacing:.12em;text-transform:uppercase;color:#FFFFFF">Support Required</span>
    <span style="font-size:10px;font-weight:500;color:#0A1628;background:#FBBF24;padding:1px 8px;border-radius:99px;margin-left:auto">{n} item{"s" if n!=1 else ""}</span>
  </div>
  <table style="width:100%;border-collapse:collapse;table-layout:fixed">
    <colgroup><col style="width:30%"><col style="width:70%"></colgroup>
    <thead>
      <tr>
        <th style="font-family:monospace;font-size:9px;font-weight:500;text-transform:uppercase;letter-spacing:.1em;color:var(--text3);text-align:left;padding:8px 16px;background:var(--surface2);border-bottom:1px solid var(--border)">Issue</th>
        <th style="font-family:monospace;font-size:9px;font-weight:500;text-transform:uppercase;letter-spacing:.1em;color:var(--text3);text-align:left;padding:8px 16px;background:var(--surface2);border-bottom:1px solid var(--border)">What I need to move forward</th>
      </tr>
    </thead>
    <tbody>{rows_html}
    </tbody>
  </table>
</div>"""

def build_html(cats, counts, total, week5_start, person_name="Carolina Corredor", person_initials="CC", person_key="carolina", person_color="#2DD4BF"):
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    max_count = max(counts) if max(counts) > 0 else 1

    # ── monthly counter
    week_cards = "".join(
        render_week_card(i, counts[i], max_count, week5_start, i == 4)
        for i in range(5)
    )

    # ── summary chips
    all_issues = (cats["deployment"] + cats["cisco"] +
                  cats["decommission"] + cats["non_deploy"])
    total_active = len(all_issues)
    in_progress = sum(1 for iss in all_issues
                      if iss["fields"]["status"]["statusCategory"]["key"] == "indeterminate")
    on_hold     = sum(1 for iss in all_issues
                      if iss["fields"]["status"]["name"] == "On Hold")
    pending     = total_active - in_progress - on_hold

    # ── gantt rows per category
    def section(emoji, label, color, issues, category):
        if not issues:
            return ""
        rows = "".join(render_issue_row(i, category) for i in issues)
        n = len(issues)
        return f"""
  <tr class="cat-row">
    <td colspan="4"><span class="cat-label" style="color:{color}">{emoji} {label} — {n} issue{"s" if n != 1 else ""}</span></td>
  </tr>{rows}"""

    gantt_rows = (
        section("🟢", "Deployment",             "#0D7F5F", cats["deployment"],   "deployment") +
        section("🟠", "Cisco upgrade project",  "#C2410C", cats["cisco"],        "cisco") +
        section("🟣", "Decommission",           "#5B21B6", cats["decommission"], "decommission") +
        section("🔵", "Non-deployment tasks",   "#0369A1", cats["non_deploy"],   "non_deploy")
    )

    MONTHS_ES = ["","Jan","Feb","Tue","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    w5_end = week5_start + timedelta(days=4)
    week_range = (f"Thu {week5_start.day} {MONTHS_ES[week5_start.month]} – "
                  f"Thu {w5_end.day} {MONTHS_ES[w5_end.month]} {w5_end.year}")

    # Build person switcher HTML
    switcher_parts = []
    for pk, pd in PEOPLE.items():
        is_active = (pk == person_key)
        c = pd["color"]
        rv,gv,bv = int(c[1:3],16), int(c[3:5],16), int(c[5:7],16)
        bg     = f"rgba({rv},{gv},{bv},.12)" if is_active else "transparent"
        border = f"rgba({rv},{gv},{bv},.4)"  if is_active else "rgba(100,116,139,.3)"
        lbl_c  = c if is_active else "#64748B"
        av_bg  = c if is_active else "#374151"
        av_fg  = "#0A1628" if is_active else "#9CA3AF"
        fname  = pd["name"].split()[0]
        av_html = (f'<span style="width:26px;height:26px;border-radius:50%;background:{av_bg};color:{av_fg};font-size:10px;font-weight:600;display:flex;align-items:center;justify-content:center">' + pd["initials"] + '</span>')
        lbl_html = f'<span style="font-size:12px;color:{lbl_c};white-space:nowrap">{fname}</span>'
        inner = f'<span style="display:flex;align-items:center;gap:7px;padding:5px 14px 5px 8px;border-radius:99px;border:1px solid {border};background:{bg};">' + av_html + lbl_html + '</span>'
        if not is_active:
            inner = f'<a href="{pd["file"]}" style="display:flex;align-items:center;gap:7px;padding:5px 14px 5px 8px;border-radius:99px;border:1px solid {border};background:{bg};text-decoration:none;">' + av_html + lbl_html + '</a>'
        switcher_parts.append(inner)
    switcher_html = "\n  ".join(switcher_parts)


    all_flat = (cats["deployment"] + cats["cisco"] +
                cats["decommission"] + cats["non_deploy"])
    support_section = render_support_section(all_flat)
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Weekly Dashboard — {person_name}</title>
{HTML_CSS}
</head>
<body>

<div class="page-wrap">
  <div class="brand-header">
    <img src="data:image/jpeg;base64,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" alt="Fibersense" style="height:52px;width:auto;object-fit:contain">
  </div>
  <div class="page-inner">
<div class="page-header">
  <div>
    <h1>Weekly Work Overview</h1>
    <p>Service Delivery - Ops (SDO) · {week_range}</p>
  </div>
  <div style="display:flex;align-items:center;gap:6px">
  {switcher_html}
  </div>
</div>

<div class="monthly">
  <div class="monthly-header">
    <span class="monthly-title">Completed Items — Current Month</span>
    <span class="monthly-badge">{total} completed this month</span>
  </div>
  <div class="weeks">{week_cards}</div>
</div>

<div class="chips">
  <span class="chip chip-total">{total_active} active issues</span>
  <span class="chip chip-ip">{in_progress} active</span>
  <span class="chip chip-hold">{on_hold} on hold</span>
  <span class="chip chip-pend">{pending} early stage</span>
</div>

<div class="gantt-wrap">
<table>
  <colgroup>
    <col class="lc">
    <col class="sc">
    <col class="pc">
    <col class="gs">
  </colgroup>
  <thead>
    <tr>
      <th class="lh"></th>
      <th style="text-align:center;letter-spacing:.1em">STATUS</th>
      <th style="text-align:left;padding-left:12px;letter-spacing:.1em">PROGRESS</th>
      <th style="text-align:center;letter-spacing:.1em">GATE</th>
    </tr>
  </thead>
  <tbody>
{gantt_rows}
  </tbody>
</table>

{support_section}

  </div>
</div>

</body>
</html>"""


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Generate Fibersense weekly dashboard")
    parser.add_argument("--open",   action="store_true", help="Open browser after generating")
    parser.add_argument("--output", default=str(OUTPUT_FILE), help="Output file path")
    parser.add_argument("--person", default="carolina", choices=list(PEOPLE.keys()),
                        help="Person: carolina | rachel  (default: carolina)")
    parser.add_argument("--all",    action="store_true",
                        help="Generate dashboards for all people")
    args = parser.parse_args()

    # Validate credentials
    global ASSIGNEE_ID
    people_to_run = list(PEOPLE.items()) if getattr(args, "all", False) else [(getattr(args, "person", "carolina"), PEOPLE[getattr(args, "person", "carolina")])]

    for person_key, person_data in people_to_run:
        ASSIGNEE_ID = person_data["id"]
        out_path    = Path(__file__).parent / person_data["file"]
        print(f"\n── {person_data['name']} ──")
        run_for_person(person_key, person_data, str(out_path), getattr(args, "open", False))
    return


def run_for_person(person_key, person_data, output_path, open_browser):
    """Full pipeline for one person."""
    global ASSIGNEE_ID
    ASSIGNEE_ID = person_data["id"]

    if not JIRA_EMAIL or not JIRA_API_TOKEN or not JIRA_BASE_URL:
        print("❌  Missing credentials. Check your .env file:")
        print("    JIRA_BASE_URL=https://your-instance.atlassian.net")
        print("    JIRA_EMAIL=your-email@company.com")
        print("    JIRA_API_TOKEN=your-api-token")
        sys.exit(1)
    if not PEOPLE["carolina"]["id"] or not PEOPLE["rachel"]["id"]:
        print("❌  Missing account IDs. Add to your .env file:")
        print("    CAROLINA_ACCOUNT_ID=...")
        print("    RACHEL_ACCOUNT_ID=...")
        sys.exit(1)

    print("⏳  Consultando Jira...")

    try:
        child_issues, task_issues = fetch_active_issues()
        counts, total, week5_start = fetch_monthly_completions()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 401:
            print("❌  Credenciales incorrectas (401). Verifica JIRA_EMAIL y JIRA_API_TOKEN en .env")
        elif code == 403:
            print("❌  Sin permisos (403). Tu token no tiene acceso al proyecto SDO.")
        else:
            print(f"❌  Error HTTP {code} desde Jira: {e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("❌  No se pudo conectar con Jira. Verifica tu conexión a internet.")
        sys.exit(1)
    except Exception as e:
        print(f"❌  Error inesperado: {e}")
        sys.exit(1)

    cats = categorize(child_issues, task_issues)

    total_found = sum(len(v) for v in cats.values())
    print(f"✅  {total_found} active issues found  |  {total} completed this month")
    print(f"    🟢 Deployment: {len(cats['deployment'])}  🟠 Cisco: {len(cats['cisco'])}  "
          f"🟣 Decommission: {len(cats['decommission'])}  🔵 Non-deployment: {len(cats['non_deploy'])}")

    html = build_html(cats, counts, total, week5_start,
                     person_name=person_data["name"],
                     person_initials=person_data["initials"],
                     person_key=person_key,
                     person_color=person_data["color"])

    out_path = Path(output_path)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n📄  Dashboard generado: {out_path.resolve()}")

    if open_browser:
        webbrowser.open(Path(output_path).resolve().as_uri())
        print("🌐  Apriendo en el browser...")


if __name__ == "__main__":
    main()
