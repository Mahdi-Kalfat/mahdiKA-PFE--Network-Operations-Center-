"""
app.py  —  MyPFE Main Application
Customer Portal (port 5000) + NOC Dashboard (/noc)

Key change from v1:
  The diagnostic + fix logic is now DETERMINISTIC Python — no LLM tool calling.
  The LLM (qwen2.5:3b) is only used to format the final friendly message.
  This removes all reliability issues with small models.
"""

import json
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
import requests
from flask import Flask, render_template, request, jsonify, session

app = Flask(__name__)
app.secret_key = "mypfe-secret-2025"

# ── Config ────────────────────────────────────────────────────────────────────
import os
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = "qwen2.5:3b"
NEO4J_API    = os.getenv("NEO4J_API",    "http://localhost:8000")
GENIE_API    = os.getenv("GENIE_API",    "http://localhost:8001")
RADUCE_API   = os.getenv("RADUCE_API",   "http://localhost:8002")
CUSTOMER_API = os.getenv("CUSTOMER_API", "http://localhost:8003")

GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_SMTP_HOST     = os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com")
GMAIL_SMTP_PORT     = int(os.getenv("GMAIL_SMTP_PORT", "587"))
PUBLIC_APP_URL      = os.getenv("PUBLIC_APP_URL", "http://localhost:5000/app")
LOGO_PATH           = os.path.join(os.path.dirname(__file__), "static", "logo.png")

# GenieACS + RaDuce are consumed as real MCP servers (tools/call over MCP).
# NOC admin endpoints (fault injection, fleet) and /health stay REST.
import mcp_link

# Role/path/fault engine — single source of truth shared with the MCP servers.
import mcp_engine as E
_resolver = E.Resolver(NEO4J_API)


# ── Generic API helper ────────────────────────────────────────────────────────

def api(method, url, **kwargs):
    try:
        kwargs.setdefault("timeout", 10)
        r = getattr(requests, method)(url, **kwargs)
        r.raise_for_status()
        return r.json(), None
    except requests.exceptions.ConnectionError:
        svc = url.split("/")[2]
        return None, f"Cannot reach {svc} — is the server running?"
    except Exception as e:
        return None, str(e)


# ── Service calls ─────────────────────────────────────────────────────────────

def genie_get_state(serial, account_id=None):
    # MCP tools/call -> genie-acs.get_router_state
    payload = {}
    if serial:
        payload["serial"] = serial
    elif account_id:
        payload["account_id"] = account_id
    return mcp_link.genie("get_router_state", payload)

def genie_get_customer(phone):
    # MCP tools/call -> genie-acs.get_customer_by_phone
    return mcp_link.genie("get_customer_by_phone", {"phone": phone})

def raduce_call(tool, serial, extra=None):
    # MCP tools/call -> raduce-acs.<tool>
    args = {"serial": serial}
    if extra:
        args.update(extra)
    return mcp_link.raduce(tool, args)

def customer_auth(phone, pin):
    # MCP tools/call -> customer.authenticate_customer
    return mcp_link.customer("authenticate_customer", {"phone": phone, "pin": pin})

def customer_create_ticket(account_id, problem, fault, status,
                            fix=None, escalation=None, full_log=None,
                            state_before=None, state_after=None,
                            resolution_time_s=None, customer=None):
    body = {
        "account_id":          account_id,
        "problem_description": problem,
        "fault_detected":      fault,
        "status":              status,
    }
    if fix:              body["fix_applied"]         = fix
    if escalation:       body["escalation_reason"]   = escalation
    if full_log:         body["full_log"]            = full_log
    if state_before:     body["router_state_before"] = state_before
    if state_after:      body["router_state_after"]  = state_after
    if resolution_time_s: body["resolution_time_s"] = resolution_time_s
    if customer:
        body["customer_name"]  = customer.get("name","")
        body["router_model"]   = customer.get("router_model","")
        body["router_serial"]  = customer.get("router_serial","")
        body["subscription_plan"] = customer.get("subscription_plan") or customer.get("subscription", "")
    return api("post", f"{CUSTOMER_API}/tools/create_ticket", json=body)

def customer_get_tickets(account_id):
    # MCP tools/call -> customer.get_customer_tickets
    return mcp_link.customer("get_customer_tickets", {"account_id": account_id})


# ── Neo4j MCP — parameter schema lookup ──────────────────────────────────────

# Cache schema per router model so we only call Neo4j once per model per session
_schema_cache: dict = {}

def neo4j_get_schema(model: str) -> dict:
    """
    Fetch all TR-069 parameter paths for a router model from Neo4j.
    Returns a flat dict:  label_lower -> {"label": str, "path": str, "category": str}
    """
    if model in _schema_cache:
        return _schema_cache[model]

    data, err = api("get", f"{NEO4J_API}/tools/get_router_parameters",
                    params={"model": model})
    if err or not data:
        return {}

    schema: dict = {}
    categories = data.get("categories", {})
    for category, params in categories.items():
        for p in params:
            label = (p.get("label") or "").strip()
            path  = (p.get("path")  or "").strip()
            if label and path:
                schema[label.lower()] = {
                    "label":    label,
                    "path":     path,
                    "category": category,
                    "editable": p.get("editable", "unknown"),
                }
    _schema_cache[model] = schema
    return schema


def neo4j_get_router_meta(model: str) -> dict:
    """Fetch router inventory metadata from Neo4j GraphRAG."""
    if not model:
        return {}
    try:
        data, err = api("get", f"{NEO4J_API}/tools/list_routers")
        if err or not data:
            return {}
        routers = data.get("routers") or []
        needle = model.lower()
        for router in routers:
            values = [
                str(router.get("id", "")).lower(),
                str(router.get("router_id", "")).lower(),
                str(router.get("product_class", "")).lower(),
                str(router.get("sheet_name", "")).lower(),
            ]
            if any(needle in value for value in values):
                normalized = dict(router)
                normalized["id"] = (
                    router.get("id")
                    or router.get("router_id")
                    or router.get("product_class")
                    or router.get("sheet_name")
                    or model
                )
                return normalized
    except Exception:
        pass
    return {}


def schema_find(schema: dict, *keywords) -> list[dict]:
    """
    Find all schema entries whose label contains ANY of the keywords.
    Returns list of matching entries sorted by label.
    """
    results = []
    seen = set()
    for key, entry in schema.items():
        for kw in keywords:
            if kw.lower() in key and entry["path"] not in seen:
                results.append(entry)
                seen.add(entry["path"])
                break
    return sorted(results, key=lambda x: x["label"])


def neo4j_diagnose(params: dict, schema: dict) -> tuple:
    """
    Diagnose router fault using Neo4j schema for path context.
    Returns (fault_name, fix_tool, fix_extra, tr069_paths_involved).
    tr069_paths_involved: list of {"label", "path", "value", "category"} 
    showing exactly which TR-069 paths led to the diagnosis.
    """
    paths_involved = []
    decision_reason = ""
    trigger = ""

    # ── Helper: find param value via schema label keywords ─────────────────
    def val(field_name: str, *label_keywords) -> str:
        """Get value from params, and record which TR-069 path was used."""
        v = str(params.get(field_name) or "")
        # Find matching paths in Neo4j schema
        matches = schema_find(schema, *label_keywords) if label_keywords else []
        for m in matches[:2]:  # record up to 2 matching paths per field
            if m["path"] not in [p["path"] for p in paths_involved]:
                paths_involved.append({
                    "label":    m["label"],
                    "path":     m["path"],
                    "value":    params.get(field_name),
                    "category": m["category"],
                })
        return v.lower()

    # ── Read values (recording TR-069 paths used) ───────────────────────────
    rx_raw = params.get("RXPower")
    try:
        rx = float(rx_raw or 0)
        # Record optical power path from schema
        rx_paths = schema_find(schema, "rx", "optical", "receive", "power")
        for p in rx_paths[:2]:
            paths_involved.append({"label": p["label"], "path": p["path"],
                                    "value": rx_raw, "category": p["category"]})
        if rx < -27:
            diag_paths = schema_find(schema, "signal", "loss", "bias")
            paths_involved += [{"label": p["label"], "path": p["path"],
                                 "value": params.get("SignalLoss"),
                                 "category": p["category"]} for p in diag_paths[:2]]
            decision_reason = "RXPower below threshold"
            trigger = "weak_signal"
            return "weak_signal", "escalate_to_technician", {}, paths_involved
    except (TypeError, ValueError):
        pass

    if params.get("SignalLoss"):
        decision_reason = "SignalLoss is true"
        trigger = "weak_signal"
        return "weak_signal", "escalate_to_technician", {}, paths_involved

    err_count = params.get("ErrorCount", 0)
    try:
        if int(err_count) > 100:
            err_paths = schema_find(schema, "error", "count", "crc")
            paths_involved += [{"label": p["label"], "path": p["path"],
                                 "value": err_count,
                                 "category": p["category"]} for p in err_paths[:2]]
            decision_reason = "ErrorCount above threshold"
            trigger = "hardware_fault"
            return "hardware_fault", "escalate_to_technician", {}, paths_involved
    except (TypeError, ValueError):
        pass

    conn   = val("ConnectionStatus",    "connection", "status", "wan")
    vlan   = params.get("VLANId")
    dns    = str(params.get("DNSServer") or "")
    dns_st = val("DNSStatus",           "dns", "domain")
    ppp    = val("PPPStatus",           "ppp", "connection", "status")
    lasterr= val("LastConnectionError", "error", "ppp", "last")

    if "unconfigured" in conn:
        vlan_paths = schema_find(schema, "vlan", "vid", "tag")
        paths_involved += [{"label": p["label"], "path": p["path"],
                             "value": vlan, "category": p["category"]} for p in vlan_paths[:2]]
        decision_reason = "ConnectionStatus is Unconfigured"
        trigger = "wrong_vlan"
        return "wrong_vlan", "set_vlan", {"vlan_id": 100}, paths_involved

    try:
        if vlan and int(vlan) not in (0, 100):
            vlan_paths = schema_find(schema, "vlan", "vid")
            paths_involved += [{"label": p["label"], "path": p["path"],
                                 "value": vlan, "category": p["category"]} for p in vlan_paths[:2]]
            decision_reason = f"VLANId {vlan} is not allowed"
            trigger = "wrong_vlan"
            return "wrong_vlan", "set_vlan", {"vlan_id": 100}, paths_involved
    except (TypeError, ValueError):
        pass

    if dns == "0.0.0.0" or "error" in dns_st:
        dns_paths = schema_find(schema, "dns", "domain", "server")
        paths_involved += [{"label": p["label"], "path": p["path"],
                             "value": dns, "category": p["category"]} for p in dns_paths[:2]]
        decision_reason = "DNS server or DNS status indicates failure"
        trigger = "dns_failure"
        return "dns_failure", "set_dns", {"primary": "8.8.8.8", "secondary": "1.1.1.1"}, paths_involved

    if "disconnect" in ppp and "authentication" in lasterr:
        ppp_paths = schema_find(schema, "ppp", "username", "password", "auth")
        paths_involved += [{"label": p["label"], "path": p["path"],
                             "value": params.get(p["label"].replace(" ","")),
                             "category": p["category"]} for p in ppp_paths[:3]]
        paths_involved.append({
            "label": "ppp_auth_failure",
            "path": "rule://ppp_auth_failure",
            "value": f"PPPStatus={ppp}, LastConnectionError={lasterr}",
            "category": "diagnostic",
        })
        decision_reason = f"PPPStatus={ppp}, LastConnectionError={lasterr}"
        trigger = "ppp_auth_failure"
        return "ppp_auth_failure", "restart_ppp", {}, paths_involved

    if "disconnect" in ppp:
        decision_reason = f"PPPStatus={ppp}"
        trigger = "random_disconnect"
        return "random_disconnect", "restart_ppp", {}, paths_involved

    decision_reason = "No fault condition matched"
    return "healthy", None, {}, paths_involved


# ── Diagnostic engine — pure Python, no LLM ──────────────────────────────────

def diagnose(params: dict):
    """Returns (fault_name, fix_tool, extra_params)."""
    ppp    = str(params.get("PPPStatus") or "").lower()
    lasterr= str(params.get("LastConnectionError") or "").lower()
    conn   = str(params.get("ConnectionStatus") or "").lower()
    dns    = str(params.get("DNSServer") or "")
    dns_st = str(params.get("DNSStatus") or "").lower()
    signal = params.get("SignalLoss", False)
    vlan   = params.get("VLANId")

    try:
        rx = float(params.get("RXPower") or 0)
        if rx < -27:
            return "weak_signal", "escalate_to_technician", {}
    except (TypeError, ValueError):
        pass

    if signal:
        return "weak_signal", "escalate_to_technician", {}

    try:
        if int(params.get("ErrorCount") or 0) > 100:
            return "hardware_fault", "escalate_to_technician", {}
    except (TypeError, ValueError):
        pass

    if "unconfigured" in conn:
        return "wrong_vlan", "set_vlan", {"vlan_id": 100}

    try:
        if vlan and int(vlan) not in (0, 100):
            return "wrong_vlan", "set_vlan", {"vlan_id": 100}
    except (TypeError, ValueError):
        pass

    if dns == "0.0.0.0" or "error" in dns_st:
        return "dns_failure", "set_dns",
        {"primary": "8.8.8.8", "secondary": "1.1.1.1"}

    if "disconnect" in ppp and "authentication" in lasterr:
        return "ppp_auth_failure", "restart_ppp", {}

    if "disconnect" in ppp:
        return "random_disconnect", "restart_ppp", {}

    return "healthy", None, {}


# ── Friendly message builder ──────────────────────────────────────────────────

MESSAGES = {
    "en": {
        "resolved_ppp":       "Your internet connection has been restored! Your router is back online. 😊",
        "resolved_vlan":      "Your router configuration has been corrected and you're back online. 😊",
        "resolved_dns":       "Your DNS settings have been fixed. Everything should work normally now. 😊",
        "resolved_generic":   "Your connection has been restored successfully. 😊",
        "escalated_signal":   "We detected a weak optical signal on your line which requires a physical technician. Someone will contact you within 24 hours. 🔧",
        "escalated_hardware": "Your router has a hardware fault that needs on-site repair. A technician will contact you within 24 hours. 🔧",
        "escalated_failed":   "We tried to fix your connection remotely but were unable to resolve the issue. A technician will contact you within 24 hours. 🔧",
        "already_ok":         "Your connection appears to be working normally right now. If the problem continues, please contact us again.",
        "error":              "We encountered a technical issue. Please try again in a few minutes.",
        "service_down":       "Our systems are temporarily unavailable. Please try again shortly.",
    },
    "fr": {
        "resolved_ppp":       "Votre connexion internet a été rétablie ! Votre routeur est à nouveau en ligne. 😊",
        "resolved_vlan":      "La configuration de votre routeur a été corrigée et vous êtes à nouveau connecté. 😊",
        "resolved_dns":       "Vos paramètres DNS ont été mis à jour. Tout devrait fonctionner normalement maintenant. 😊",
        "resolved_generic":   "Votre connexion a été rétablie avec succès. 😊",
        "escalated_signal":   "Nous avons détecté un signal optique faible sur votre ligne, ce qui nécessite l'intervention d'un technicien. Vous serez contacté dans les 24 heures. 🔧",
        "escalated_hardware": "Votre routeur présente une panne matérielle nécessitant une intervention sur site. Un technicien vous contactera dans les 24 heures. 🔧",
        "escalated_failed":   "Nous avons tenté de corriger votre connexion à distance sans succès. Un technicien vous contactera dans les 24 heures. 🔧",
        "already_ok":         "Votre connexion semble fonctionner normalement en ce moment. Si le problème persiste, n'hésitez pas à nous recontacter.",
        "error":              "Nous rencontrons un problème technique. Veuillez réessayer dans quelques minutes.",
        "service_down":       "Nos systèmes sont temporairement indisponibles. Veuillez réessayer dans quelques instants.",
    }
}

def get_msg(lang, key, name=""):
    msgs = MESSAGES.get(lang, MESSAGES["en"])
    text = msgs.get(key, msgs["error"])
    if name:
        first = name.split()[0]
        prefix = f"Bonjour {first}, " if lang == "fr" else f"Hi {first}, "
        text = prefix + text[0].lower() + text[1:]
    return text


# ── Email sending (Gmail SMTP, HTML templates + inline logo) ──────────────────

def _send_email(to_email: str, subject: str, text_body: str, html_body: str = None) -> bool:
    """Send an email via Gmail SMTP (HTML with plain-text fallback, logo inlined
    via Content-ID). Falls back to logging the message to the console when no
    Gmail credentials are configured, so email-dependent flows stay testable
    before real credentials are dropped in."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print(f"[email-stub] No GMAIL_USER/GMAIL_APP_PASSWORD configured — "
              f"email to {to_email} not sent.\nSubject: {subject}\n{text_body}")
        return True

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = to_email

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    if html_body:
        alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    if html_body and os.path.exists(LOGO_PATH):
        with open(LOGO_PATH, "rb") as f:
            logo = MIMEImage(f.read())
        logo.add_header("Content-ID", "<byrsa_logo>")
        logo.add_header("Content-Disposition", "inline", filename="logo.png")
        msg.attach(logo)

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email] Failed to send email to {to_email}: {e}")
        return False


def send_verification_email(to_email: str, code: str, name: str, lang: str) -> bool:
    first = (name or "").split(" ")[0]
    if lang == "fr":
        subject = "Votre code de vérification Byrsa"
        text_body = (f"Bonjour {first},\n\nVotre code de vérification Byrsa est : {code}\n\n"
                    "Ce code expire dans 10 minutes.\n\n— L'équipe Byrsa")
        tpl = dict(lang="fr", subject=subject, eyebrow="VÉRIFICATION DE COMPTE",
                  greeting=f"Bonjour {first},",
                  intro="Utilisez le code ci-dessous pour vérifier votre adresse email et accéder à votre espace Byrsa Support.",
                  code=code,
                  expiry_note="Ce code expire dans 10 minutes. Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet email.",
                  footer_note="Byrsa · Smart Customer Care — Cet email a été envoyé automatiquement, merci de ne pas y répondre.")
    else:
        subject = "Your Byrsa verification code"
        text_body = (f"Hi {first},\n\nYour Byrsa verification code is: {code}\n\n"
                    "This code expires in 10 minutes.\n\n— The Byrsa Team")
        tpl = dict(lang="en", subject=subject, eyebrow="ACCOUNT VERIFICATION",
                  greeting=f"Hi {first},",
                  intro="Use the code below to verify your email address and unlock your Byrsa Support account.",
                  code=code,
                  expiry_note="This code expires in 10 minutes. If you didn't request this, you can safely ignore this email.",
                  footer_note="Byrsa · Smart Customer Care — This is an automated message, please don't reply.")
    html_body = render_template("email/verify.html", **tpl)
    return _send_email(to_email, subject, text_body, html_body)


# ── Fault knowledge base — used by the notification bell / proactive bot ──────

FAULT_INFO = {
    "ppp_auth_failure": {
        "en": {"title": "PPP Authentication Failure",
               "description": "Your router failed to authenticate its PPP session with our network.",
               "cause": "The stored PPP username/password was rejected by the authentication server, or the session token expired."},
        "fr": {"title": "Échec d'authentification PPP",
               "description": "Votre routeur n'a pas réussi à authentifier sa session PPP sur notre réseau.",
               "cause": "L'identifiant/mot de passe PPP enregistré a été rejeté par le serveur d'authentification, ou le jeton de session a expiré."},
    },
    "wrong_vlan": {
        "en": {"title": "VLAN Misconfiguration",
               "description": "Your router is tagging traffic with an incorrect VLAN ID, so it can't reach our network.",
               "cause": "A misapplied provisioning profile set the VLAN ID outside the range allowed for your line."},
        "fr": {"title": "Erreur de configuration VLAN",
               "description": "Votre routeur étiquette le trafic avec un identifiant VLAN incorrect, ce qui l'empêche d'atteindre notre réseau.",
               "cause": "Un profil de provisioning mal appliqué a défini un VLAN en dehors de la plage autorisée pour votre ligne."},
    },
    "dns_failure": {
        "en": {"title": "DNS Resolution Failure",
               "description": "Your router can reach the network but its DNS server settings are invalid.",
               "cause": "The DNS server address was cleared or set to an unreachable value, so domain names can't be resolved."},
        "fr": {"title": "Échec de résolution DNS",
               "description": "Votre routeur accède au réseau mais ses serveurs DNS sont invalides.",
               "cause": "L'adresse du serveur DNS a été effacée ou pointe vers une valeur injoignable, empêchant la résolution des noms de domaine."},
    },
    "weak_signal": {
        "en": {"title": "Weak Optical Signal",
               "description": "The optical signal reaching your router is too weak for a stable connection.",
               "cause": "This is usually caused by a fiber connector issue, a bent cable, or line degradation and needs a physical check."},
        "fr": {"title": "Signal optique faible",
               "description": "Le signal optique reçu par votre routeur est trop faible pour une connexion stable.",
               "cause": "Généralement causé par un connecteur fibre défectueux, un câble plié ou une dégradation de ligne — une intervention physique est nécessaire."},
    },
    "hardware_fault": {
        "en": {"title": "Hardware Fault",
               "description": "Your router is reporting an abnormally high error rate consistent with a hardware issue.",
               "cause": "Internal component degradation on the router is corrupting traffic — a technician visit is required."},
        "fr": {"title": "Panne matérielle",
               "description": "Votre routeur signale un taux d'erreurs anormalement élevé, révélateur d'un problème matériel.",
               "cause": "La dégradation d'un composant interne du routeur corrompt le trafic — une visite technique est nécessaire."},
    },
    "random_disconnect": {
        "en": {"title": "Connection Drop",
               "description": "Your router's PPP session dropped unexpectedly.",
               "cause": "A transient network-side interruption closed the session without an authentication error."},
        "fr": {"title": "Coupure de connexion",
               "description": "La session PPP de votre routeur s'est interrompue de façon inattendue.",
               "cause": "Une coupure transitoire côté réseau a fermé la session sans erreur d'authentification."},
    },
    "healthy": {
        "en": {"title": "No Fault Detected",
               "description": "Your connection currently looks healthy.",
               "cause": "No fault condition matched the live router parameters."},
        "fr": {"title": "Aucune anomalie détectée",
               "description": "Votre connexion semble actuellement fonctionner normalement.",
               "cause": "Aucune condition de panne ne correspond aux paramètres actuels du routeur."},
    },
}


# ── Notification bell — persisted via the customer MCP server (MongoDB) ───────

PENDING_FIXES: dict = {}     # account_id -> diagnosis awaiting a fix confirmation (in-process, transient)

def send_notification_email(to_email: str, name: str, fault: str, lang: str) -> bool:
    info = FAULT_INFO.get(fault, FAULT_INFO["healthy"])
    fi   = info.get(lang, info["en"])
    first = (name or "").split(" ")[0]
    if lang == "fr":
        subject = "Byrsa — Problème détecté sur votre routeur"
        text_body = (f"Bonjour {first},\n\nNous avons détecté un problème avec votre routeur : {fi['title']}.\n\n"
                    f"{fi['description']}\n\nConnectez-vous à votre espace Byrsa pour en savoir plus "
                    "et discuter avec notre assistant.\n\n— L'équipe Byrsa")
        tpl = dict(lang="fr", subject=subject, eyebrow="ALERTE ROUTEUR",
                  greeting=f"Bonjour {first},",
                  intro="Notre système a détecté un problème sur votre connexion. Voici ce que nous avons trouvé :",
                  fault_label_head="PROBLÈME DÉTECTÉ", fault_title=fi["title"], description=fi["description"],
                  cta="Ouvrir Byrsa Support", app_url=PUBLIC_APP_URL,
                  footer_note="Byrsa · Smart Customer Care — Cet email a été envoyé automatiquement, merci de ne pas y répondre.")
    else:
        subject = "Byrsa — We detected a problem with your router"
        text_body = (f"Hi {first},\n\nWe detected a problem with your router: {fi['title']}.\n\n"
                    f"{fi['description']}\n\nLog in to your Byrsa account to see more details "
                    "and chat with our assistant.\n\n— The Byrsa Team")
        tpl = dict(lang="en", subject=subject, eyebrow="ROUTER ALERT",
                  greeting=f"Hi {first},",
                  intro="Our system detected a problem on your connection. Here's what we found:",
                  fault_label_head="ISSUE DETECTED", fault_title=fi["title"], description=fi["description"],
                  cta="Open Byrsa Support", app_url=PUBLIC_APP_URL,
                  footer_note="Byrsa · Smart Customer Care — This is an automated message, please don't reply.")
    html_body = render_template("email/notify.html", **tpl)
    return _send_email(to_email, subject, text_body, html_body)


def add_notification(account_id: str, customer_name: str, fault: str):
    info = FAULT_INFO.get(fault, FAULT_INFO["healthy"])
    api("post", f"{CUSTOMER_API}/notifications/create", json={
        "account_id":     account_id,
        "customer_name":  customer_name,
        "fault":          fault,
        "fault_label_en": info["en"]["title"],
        "fault_label_fr": info["fr"]["title"],
    })

    profile, err = api("get", f"{CUSTOMER_API}/customer/profile", params={"account_id": account_id})
    if not err and profile and profile.get("email_verified") and profile.get("email"):
        send_notification_email(profile["email"], profile.get("name") or customer_name, fault, "en")

def get_notifications(account_id: str):
    data, err = api("get", f"{CUSTOMER_API}/notifications", params={"account_id": account_id})
    if err or not data:
        return []
    return data.get("notifications", [])


# ── Main support flow ─────────────────────────────────────────────────────────

def _run_diagnosis(customer: dict):
    """Layers 1-2b only: read live state, load TR-069 schema, diagnose.
    Does NOT push any fix. Returns a dict with the diagnostic trace so far."""
    serial       = customer.get("router_serial", "")
    account_id   = customer.get("account_id", "")
    steps        = []

    graph_rag_log = customer.get("graph_rag_log") or {
        "source": "Neo4j GraphRAG",
        "router_model": customer.get("router_model", ""),
        "router_meta": {
            "id": neo4j_get_router_meta(customer.get("router_model", "")).get("id"),
            "product_class": neo4j_get_router_meta(customer.get("router_model", "")).get("product_class"),
            "sheet_name": neo4j_get_router_meta(customer.get("router_model", "")).get("sheet_name"),
        },
        "router_serial": customer.get("router_serial", ""),
        "subscription_plan": customer.get("subscription_plan") or customer.get("subscription", ""),
    }
    if graph_rag_log:
        steps.append({
            "layer": "GraphRAG Lookup", "port": 8000,
            "tool": "list_routers",
            "description": "Resolve router metadata from Neo4j GraphRAG",
            "args": {"model": customer.get("router_model", "")},
            "result": graph_rag_log,
            "ok": True,
        })

    # LAYER 1: GenieACS MCP (port 8001) — read live router state
    state, err = genie_get_state(serial, account_id)
    state_before = state
    steps.append({
        "layer": "GenieACS MCP", "port": 8001,
        "tool": "get_router_state",
        "description": "Read live router parameters from ACS database",
        "args": {"serial": serial},
        "result": state or {"error": err},
        "ok": err is None
    })

    if err or not state:
        return {"steps": steps, "state_before": state_before,
                "fault": None, "fix_tool": None, "fix_extra": {},
                "error": "service_down"}

    params = state.get("parameters", {})
    router_model = customer.get("router_model", "")

    # LAYER 2a: Neo4j MCP (port 8000) — fetch TR-069 parameter schema
    schema, schema_err = api("get", f"{NEO4J_API}/tools/get_router_parameters",
                             params={"model": router_model})

    # Detect Neo4j-level errors: the API returns {"error": "..."} in the body
    # even when the HTTP call itself succeeds (schema_err stays None).
    neo4j_body_err = None
    if schema and schema.get("error"):
        neo4j_body_err = schema["error"]

    neo4j_schema = {}
    if schema and not schema_err and not neo4j_body_err:
        for category, param_list in (schema.get("categories") or {}).items():
            for p in param_list:
                label = (p.get("label") or "").strip()
                path  = (p.get("path")  or "").strip()
                if label and path:
                    neo4j_schema[label.lower()] = {
                        "label": label, "path": path,
                        "category": category, "editable": p.get("editable", "unknown")
                    }

    # Determine step result and ok flag:
    # - If Neo4j service is unreachable → schema_err is set → ok=False (real failure)
    # - If Neo4j is reachable but model not found → neo4j_body_err set → ok=True (warning, not a blocker)
    # - If parameters loaded → ok=True
    effective_err  = schema_err  # only a real transport/HTTP error
    neo4j_step_ok  = effective_err is None  # service reachable = ok; model-not-found is a warning
    loaded_params = schema.get("total_params") if schema else 0

    steps.append({
        "layer": "Neo4j MCP", "port": 8000,
        "tool": "get_router_parameters",
        "description": f"Fetch TR-069 parameter schema for {router_model} — {loaded_params} parameters loaded",
        "args": {"model": router_model},
        "result": {
            "router_model":   schema.get("router_id") if schema else None,
            "total_params":   schema.get("total_params") if schema else 0,
            "loaded_params":  loaded_params,
            "parsed_params":   len(neo4j_schema),
            "categories":     list((schema.get("categories") or {}).keys()) if schema else [],
            "schema_loaded":  len(neo4j_schema) > 0,
            "warning":        neo4j_body_err,   # model not found is a warning, not an error
            "error":          effective_err,
        },
        "ok": neo4j_step_ok
    })

    # LAYER 2b: Diagnostic Engine — graph-driven, reads values by role -> real path
    diag = E.diagnose(router_model, params, _resolver)
    fault     = diag["fault"]
    fix_tool  = diag["fix_tool"]
    fix_extra = diag["fix_extra"]
    tr069_paths = diag["paths_read"]   # [{role, path, value}] — the GraphRAG trace
    steps.append({
        "layer": "Diagnostic Engine", "port": 5000,
        "tool": "diagnose",
        "description": "Cross-reference live values (read by role -> TR-069 path) to identify the fault",
        "args": {p["role"]: p["value"] for p in tr069_paths},
        "result": {
            "fault_detected":       fault,
            "fix_tool":             fix_tool,
            "fix_params":           fix_extra,
            "tr069_paths_read":     tr069_paths,
        },
        "ok": True
    })

    return {"steps": steps, "state_before": state_before,
            "fault": fault, "fix_tool": fix_tool, "fix_extra": fix_extra,
            "error": None}


def investigate_problem(customer: dict, lang: str):
    """Diagnose only (no fix applied) — used by the proactive notification bot."""
    diag = _run_diagnosis(customer)
    name = customer.get("name", "")
    if diag["error"]:
        return {"ok": False, "message": get_msg(lang, diag["error"], name), "steps": diag["steps"]}

    fault = diag["fault"] or "healthy"
    info  = FAULT_INFO.get(fault, FAULT_INFO["healthy"])
    fi    = info.get(lang, info["en"])
    fixable = fault != "healthy" and diag["fix_tool"] is not None

    return {
        "ok": True, "fault": fault, "fix_tool": diag["fix_tool"], "fix_extra": diag["fix_extra"],
        "title": fi["title"], "description": fi["description"], "cause": fi["cause"],
        "steps": diag["steps"], "state_before": diag["state_before"], "fixable": fixable,
    }


def handle_problem(customer: dict, problem: str, lang: str):
    serial     = customer.get("router_serial", "")
    account_id = customer.get("account_id", "")
    name       = customer.get("name", "")
    start_time = time.time()

    if not serial and not account_id:
        return get_msg(lang, "error", name), []

    diag = _run_diagnosis(customer)
    steps = diag["steps"]

    if diag["error"]:
        customer_create_ticket(account_id, problem, "unknown", "open",
                               full_log=steps, customer=customer)
        return get_msg(lang, diag["error"], name), steps

    fault, fix_tool, fix_extra = diag["fault"], diag["fix_tool"], diag["fix_extra"]
    state_before = diag["state_before"]

    if fault == "healthy" or fix_tool is None:
        duration = round(time.time() - start_time, 2)
        ticket, _ = customer_create_ticket(account_id, problem, "healthy",
                               "resolved", fix="no_action_needed",
                               full_log=steps, state_before=state_before,
                               resolution_time_s=duration, customer=customer)
        return get_msg(lang, "already_ok", name), steps

    return _apply_fix(customer, problem, fault, fix_tool, fix_extra,
                       steps, state_before, lang, start_time)


def _apply_fix(customer: dict, problem: str, fault: str, fix_tool: str, fix_extra: dict,
               steps: list, state_before: dict, lang: str, start_time: float):
    """Layers 3-4: push the fix, verify, create the resulting ticket."""
    serial     = customer.get("router_serial", "")
    account_id = customer.get("account_id", "")
    name       = customer.get("name", "")

    # LAYER 2a rebuild — Neo4j schema is needed here to find TR-069 write paths
    router_model = customer.get("router_model", "")
    schema, _ = api("get", f"{NEO4J_API}/tools/get_router_parameters",
                    params={"model": router_model})
    neo4j_schema = {}
    if schema and not schema.get("error"):
        for category, param_list in (schema.get("categories") or {}).items():
            for p in param_list:
                label = (p.get("label") or "").strip()
                path  = (p.get("path")  or "").strip()
                if label and path:
                    neo4j_schema[label.lower()] = {
                        "label": label, "path": path,
                        "category": category, "editable": p.get("editable", "unknown")
                    }

    # LAYER 3: RaDuce MCP (port 8002) — push fix to router
    # Find TR-069 write paths for this fix from Neo4j schema
    write_path_keywords = {
        "restart_ppp": ["ppp", "connection", "enable", "reset"],
        "set_vlan":    ["vlan", "vid", "tag"],
        "set_dns":     ["dns", "domain", "server", "name"],
        "reboot_router": ["reboot", "reset", "restart"],
    }
    write_paths = []
    if fix_tool in write_path_keywords:
        kws = write_path_keywords[fix_tool]
        for lbl, entry in neo4j_schema.items():
            if any(k in lbl for k in kws) and entry.get("editable") in ("write", "readwrite", "unknown"):
                write_paths.append({"label": entry["label"], "path": entry["path"],
                                    "category": entry["category"]})
                if len(write_paths) >= 4:
                    break

    fix_result, fix_err = raduce_call(fix_tool, serial,
                                       {**fix_extra,
                                        "account_id": account_id,
                                        "reason": fault} if fix_tool == "escalate_to_technician"
                                       else fix_extra)
    steps.append({
        "layer": "RaDuce MCP", "port": 8002,
        "tool": fix_tool,
        "description": {
            "restart_ppp": "Restart PPP session — send SetParameterValues for PPP credentials",
            "set_vlan": "Correct VLAN ID — send SetParameterValues for VLAN configuration",
            "set_dns": "Update DNS — send SetParameterValues for DNS server addresses",
            "reboot_router": "Remote reboot — send Reboot RPC to router",
            "escalate_to_technician": "Escalate — create open ticket for physical intervention",
        }.get(fix_tool, "Apply remote fix"),
        "args": {"serial": serial, **fix_extra},
        "tr069_paths_written": write_paths,
        "result": fix_result or {"error": fix_err},
        "ok": fix_err is None and bool(fix_result)
    })

    # Escalation path
    if fix_tool == "escalate_to_technician":
        duration = round(time.time() - start_time, 2)
        ticket, _ = customer_create_ticket(account_id, problem, fault,
                                            "escalated", escalation=fault,
                                            full_log=steps, state_before=state_before,
                                            resolution_time_s=duration, customer=customer)
        msg_key = "escalated_signal" if fault == "weak_signal" else "escalated_hardware"
        msg = get_msg(lang, msg_key, name)
        if ticket and ticket.get("short_id"):
            msg += f'\n\n🎫 Ticket #{ticket["short_id"]}'
        return msg, steps

    # LAYER 4: GenieACS MCP (port 8001) — verify fix worked
    state2 = None
    verification_attempts = 3
    for attempt in range(verification_attempts):
        time.sleep(0.5)
        state2, _ = genie_get_state(serial, account_id)
        if state2 and state2.get("status") == "UP":
            break

    steps.append({
        "layer": "GenieACS MCP", "port": 8001,
        "tool": "get_router_state",
        "description": (
            "Verify fix — re-read router state to confirm recovery "
            f"(attempt {attempt + 1}/{verification_attempts})"
        ),
        "args": {"serial": serial, "account_id": account_id},
        "result": state2 or {},
        "ok": state2 is not None and state2.get("status") == "UP"
    })

    fix_success = isinstance(fix_result, dict) and fix_result.get("success") is True
    if state2 and state2.get("status") == "UP":
        fix_success = True

    if fix_success:
        duration = round(time.time() - start_time, 2)
        msg_key = {
            "restart_ppp": "resolved_ppp",
            "set_vlan":    "resolved_vlan",
            "set_dns":     "resolved_dns",
        }.get(fix_tool, "resolved_generic")
        ticket, _ = customer_create_ticket(account_id, problem, fault,
                                            "resolved", fix=fix_tool,
                                            full_log=steps, state_before=state_before,
                                            state_after=state2 if state2 else None,
                                            resolution_time_s=duration,
                                            customer=customer)
        msg = get_msg(lang, msg_key, name)
        if ticket and ticket.get("short_id"):
            msg += f'\n\n🎫 Ticket #{ticket["short_id"]}'
        return msg, steps

    # Fix failed — escalate
    esc, _ = raduce_call("escalate_to_technician", serial,
                          {"account_id": account_id,
                           "reason": f"Remote fix ({fix_tool}) failed — router still DOWN"})
    steps.append({
        "layer": "RaDuce MCP", "port": 8002,
        "tool": "escalate_to_technician",
        "description": "Remote fix failed — escalate to physical technician",
        "args": {"serial": serial, "reason": f"Remote fix ({fix_tool}) failed"},
        "result": esc or {},
        "ok": esc is not None
    })
    duration = round(time.time() - start_time, 2)
    ticket, _ = customer_create_ticket(account_id, problem, fault,
                                        "escalated",
                                        escalation=f"Remote fix failed: {fix_tool}",
                                        full_log=steps, state_before=state_before,
                                        resolution_time_s=duration, customer=customer)
    msg = get_msg(lang, "escalated_failed", name)
    if ticket and ticket.get("short_id"):
        msg += f'\n\n🎫 Ticket #{ticket["short_id"]}'
    return msg, steps


# ── Intent detection ──────────────────────────────────────────────────────────

def is_history_request(msg):
    kw = ["ticket", "history", "historique", "passé", "previous",
          "ancien", "mes tickets", "my tickets"]
    return any(k in msg.lower() for k in kw)


# ── Status helper ─────────────────────────────────────────────────────────────

def get_status():
    status = {"ollama": False, "neo4j_mcp": False,
              "genie": False, "raduce": False, "customer": False}
    for key, url in [
        ("neo4j_mcp", f"{NEO4J_API}/health"),
        ("genie",     f"{GENIE_API}/health"),
        ("raduce",    f"{RADUCE_API}/health"),
        ("customer",  f"{CUSTOMER_API}/health"),
    ]:
        try:
            r = requests.get(url, timeout=2)
            status[key] = r.json().get("status") in ("ok", "degraded")
        except Exception:
            pass
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = [m["name"] for m in r.json().get("models", [])]
        status["ollama"] = any(OLLAMA_MODEL in m for m in models)
    except Exception:
        pass
    return status


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES — Customer Portal
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/app")
def index():
    return render_template("customer.html")


@app.route("/api/status")
def api_status():
    return jsonify(get_status())


@app.route("/api/login", methods=["POST"])
def api_login():
    data  = request.json or {}
    phone = data.get("phone", "").strip()
    pin   = data.get("pin", "").strip()
    lang  = data.get("lang", "en")
    if not phone or not pin:
        return jsonify({"success": False, "error": "Phone and PIN required"}), 400
    result, err = customer_auth(phone, pin)
    if err:
        return jsonify({"success": False, "error": err}), 503
    if result and result.get("authenticated"):
        session["customer"] = result
        session["lang"]     = lang
        return jsonify({"success": True, "customer": result})
    return jsonify({"success": False,
                    "error": (result or {}).get("error", "Incorrect phone or PIN")}), 401


@app.route("/api/email/start", methods=["POST"])
def api_email_start():
    """Save the customer's email and send a fresh 6-digit verification code."""
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401
    lang  = session.get("lang", "en")
    email = (request.json or {}).get("email", "").strip()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"success": False, "error": "Please enter a valid email address"}), 400

    result, err = api("post", f"{CUSTOMER_API}/customer/email",
                      json={"account_id": customer["account_id"], "email": email})
    if err or not result or not result.get("success"):
        return jsonify({"success": False,
                        "error": (result or {}).get("error") or err or "Failed to save email"}), 503

    emailed = send_verification_email(email, result["code"], customer.get("name", ""), lang)
    session["customer"]["email"]          = email
    session["customer"]["email_verified"] = False
    session.modified = True
    return jsonify({"success": True, "emailed": emailed})


@app.route("/api/email/verify", methods=["POST"])
def api_email_verify():
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401
    code = (request.json or {}).get("code", "").strip()
    result, err = api("post", f"{CUSTOMER_API}/customer/email/verify",
                      json={"account_id": customer["account_id"], "code": code})
    if err:
        return jsonify({"success": False, "error": err}), 503
    if result and result.get("success"):
        session["customer"]["email_verified"] = True
        session.modified = True
    return jsonify(result)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data     = request.json or {}
    message  = data.get("message", "").strip()
    lang     = session.get("lang", data.get("lang", "en"))
    customer = session.get("customer")
    if not message:
        return jsonify({"error": "Empty message"}), 400
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401

    if is_history_request(message):
        tickets, err = customer_get_tickets(customer["account_id"])
        if err or not tickets or not tickets.get("tickets"):
            answer = "No tickets found." if lang == "en" else "Aucun ticket trouvé."
        else:
            t_list = tickets["tickets"][:5]
            if lang == "fr":
                lines = [f"**Vos {len(t_list)} dernier(s) ticket(s) :**"]
                for t in t_list:
                    lines.append(f"• {t.get('problem_description','?')} — **{t.get('status','?')}**")
            else:
                lines = [f"**Your {len(t_list)} recent ticket(s):**"]
                for t in t_list:
                    lines.append(f"• {t.get('problem_description','?')} — **{t.get('status','?')}**")
            answer = "\n".join(lines)
        return jsonify({"answer": answer, "steps": []})

    answer, steps = handle_problem(customer, message, lang)
    return jsonify({"answer": answer, "steps": steps})


@app.route("/api/tickets")
def api_tickets():
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401
    data, err = customer_get_tickets(customer["account_id"])
    if err:
        return jsonify({"error": err}), 503
    return jsonify(data)


@app.route("/api/notifications")
def api_notifications():
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401
    lang  = session.get("lang", "en")
    items = get_notifications(customer["account_id"])
    out = [{
        "id":          n["id"],
        "fault":       n["fault"],
        "fault_label": n.get(f"fault_label_{lang}") or n.get("fault_label_en", ""),
        "read":        n["read"],
        "created_at":  n["created_at"],
    } for n in items]
    return jsonify({"notifications": out, "unread": sum(1 for n in items if not n["read"])})


@app.route("/api/notifications/<nid>/read", methods=["POST"])
def api_notification_read(nid):
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401
    api("post", f"{CUSTOMER_API}/notifications/{nid}/read")
    return jsonify({"success": True})


@app.route("/api/notifications/<nid>", methods=["DELETE"])
def api_notification_delete(nid):
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401
    data, err = api("delete", f"{CUSTOMER_API}/notifications/{nid}",
                    params={"account_id": customer["account_id"]})
    if err:
        return jsonify({"error": err}), 503
    return jsonify(data)


@app.route("/api/investigate", methods=["POST"])
def api_investigate():
    """Proactive bot flow, step 1: diagnose the fault without applying any fix."""
    data     = request.json or {}
    lang     = session.get("lang", data.get("lang", "en"))
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401

    result = investigate_problem(customer, lang)
    if result["ok"]:
        PENDING_FIXES[customer["account_id"]] = {
            "fault":        result["fault"],
            "fix_tool":     result["fix_tool"],
            "fix_extra":    result["fix_extra"],
            "steps":        result["steps"],
            "state_before": result["state_before"],
        }
    return jsonify(result)


@app.route("/api/fix", methods=["POST"])
def api_fix():
    """Proactive bot flow, step 2: apply the fix from the last /api/investigate call."""
    data     = request.json or {}
    lang     = session.get("lang", data.get("lang", "en"))
    customer = session.get("customer")
    if not customer:
        return jsonify({"error": "Not authenticated"}), 401

    pending = PENDING_FIXES.pop(customer["account_id"], None)
    if not pending:
        return jsonify({"error": "No investigation in progress — ask me to investigate first."}), 400

    problem = f"Proactive fault notification: {pending['fault']}"
    if not pending["fault"] or pending["fault"] == "healthy" or not pending["fix_tool"]:
        duration = 0.0
        customer_create_ticket(customer["account_id"], problem, pending["fault"] or "healthy",
                               "resolved", fix="no_action_needed", full_log=pending["steps"],
                               state_before=pending["state_before"], resolution_time_s=duration,
                               customer=customer)
        return jsonify({"answer": get_msg(lang, "already_ok", customer.get("name", "")),
                        "steps": pending["steps"]})

    start_time = time.time()
    msg, steps = _apply_fix(customer, problem, pending["fault"], pending["fix_tool"],
                            pending["fix_extra"], pending["steps"], pending["state_before"],
                            lang, start_time)
    return jsonify({"answer": msg, "steps": steps})


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES — NOC Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/noc")
def noc_index():
    return render_template("noc.html")


@app.route("/api/noc/login", methods=["POST"])
def api_noc_login():
    data = request.json or {}
    result, err = api("get", f"{CUSTOMER_API}/admin/authenticate",
                      params={"username": data.get("username",""),
                              "password": data.get("password","")})
    if err:
        return jsonify({"success": False, "error": err}), 503
    if result and result.get("authenticated"):
        session["admin"] = result
        return jsonify({"success": True, "admin": result})
    return jsonify({"success": False,
                    "error": (result or {}).get("error","Login failed")}), 401


@app.route("/api/noc/logout", methods=["POST"])
def api_noc_logout():
    session.pop("admin", None)
    return jsonify({"success": True})


@app.route("/api/noc/fleet")
def api_noc_fleet():
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    data, err = api("get", f"{GENIE_API}/noc/fleet")
    if err: return jsonify({"error": err}), 503
    return jsonify(data)


@app.route("/api/noc/router_state")
def api_noc_router_state():
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    params = {}
    if request.args.get("serial"):     params["serial"]     = request.args["serial"]
    if request.args.get("account_id"): params["account_id"] = request.args["account_id"]
    data, err = mcp_link.genie("get_router_state", params)
    if err: return jsonify({"error": err}), 503
    return jsonify(data)


@app.route("/api/noc/inject_fault", methods=["POST"])
def api_noc_inject():
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    data, err = api("post", f"{GENIE_API}/noc/inject_fault", json=request.json or {})
    if err: return jsonify({"error": err}), 503

    if data and data.get("success") and data.get("fault_injected") not in (None, "healthy"):
        fleet, fleet_err = api("get", f"{GENIE_API}/noc/fleet")
        if not fleet_err and fleet:
            row = next((r for r in fleet.get("routers", [])
                       if r.get("serial") == data.get("serial")), None)
            if row and row.get("account_id"):
                add_notification(row["account_id"], row.get("customer_name", ""),
                                 data["fault_injected"])

    return jsonify(data)


@app.route("/api/noc/clear_fault", methods=["POST"])
def api_noc_clear():
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    data, err = api("post", f"{GENIE_API}/noc/clear_fault", json=request.json or {})
    if err: return jsonify({"error": err}), 503
    return jsonify(data)


@app.route("/api/noc/tickets")
def api_noc_tickets():
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    params = {"limit": 50}
    if request.args.get("status"): params["status"] = request.args["status"]
    data, err = api("get", f"{CUSTOMER_API}/admin/tickets", params=params)
    if err: return jsonify({"error": err}), 503
    return jsonify(data)


@app.route("/api/noc/tickets/create", methods=["POST"])
def api_noc_create_ticket():
    """NOC admin creates a manual ticket (no diagnostic steps)."""
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    
    data = request.json or {}
    account_id = data.get("account_id", "").strip()
    problem = data.get("problem", "").strip()
    fault = data.get("fault", "manual").strip()
    status = data.get("status", "open").strip()
    
    if not account_id or not problem:
        return jsonify({"error": "Missing account_id or problem"}), 400
    
    result, err = customer_create_ticket(
        account_id=account_id,
        problem=problem,
        fault=fault,
        status=status,
        full_log=[{"layer": "NOC Manual Entry", "port": 5000,
                   "description": f"Ticket created manually by {session['admin'].get('name', 'NOC admin')}",
                   "result": {}, "ok": True}]
    )
    if err:
        return jsonify({"error": err}), 503
    return jsonify(result)



@app.route("/noc/tickets")
def noc_tickets_page():
    if not session.get("admin"):
        return render_template("noc.html")
    return render_template("tickets.html")


@app.route("/api/noc/ticket/<ticket_id>")
def api_noc_ticket_detail(ticket_id):
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    data, err = api("get", f"{CUSTOMER_API}/admin/ticket/{ticket_id}")
    if err: return jsonify({"error": err}), 503
    return jsonify(data)


@app.route("/api/noc/tickets/search")
def api_noc_tickets_search():
    if not session.get("admin"):
        return jsonify({"error": "Not authenticated"}), 401
    status  = request.args.get("status")
    fault   = request.args.get("fault")
    account = request.args.get("account")
    limit   = int(request.args.get("limit", 100))
    params  = {"limit": limit}
    if status:  params["status"]  = status
    if fault:   params["fault"]   = fault
    if account: params["account"] = account
    data, err = api("get", f"{CUSTOMER_API}/admin/tickets", params=params)
    if err: return jsonify({"error": err}), 503
    return jsonify(data)

# ── Debug endpoint ────────────────────────────────────────────────────────────

@app.route("/api/debug")
def api_debug():
    """Open http://localhost:5000/api/debug to check all services at once."""
    out = {}
    for name, url in [
        ("neo4j",    f"{NEO4J_API}/health"),
        ("genie",    f"{GENIE_API}/health"),
        ("raduce",   f"{RADUCE_API}/health"),
        ("customer", f"{CUSTOMER_API}/health"),
    ]:
        d, e = api("get", url)
        out[name] = d or {"error": e}

    # Test auth
    d, e = api("get", f"{CUSTOMER_API}/tools/authenticate_customer",
               params={"phone": "0661234567", "pin": "1234"})
    out["test_auth"] = d or {"error": e}

    # Test profile + state
    d, e = genie_get_customer("0661234567")
    if d:
        serial = d.get("router_serial")
        out["test_profile"] = {"serial": serial, "model": d.get("router_model")}
        d2, e2 = genie_get_state(serial)
        out["test_state"] = {
            "status": (d2 or {}).get("status"),
            "fault":  (d2 or {}).get("fault"),
            "ppp":    (d2 or {}).get("parameters", {}).get("PPPStatus"),
            "error":  e2,
        } if d2 else {"error": e2}
    else:
        out["test_profile"] = {"error": e}

    # Test tickets
    d, e = api("get", f"{CUSTOMER_API}/admin/tickets", params={"limit": 5})
    out["test_tickets"] = {
        "total": (d or {}).get("total", 0),
        "error": e,
        "first_ticket_keys": list((d or {}).get("tickets", [{}])[0].keys()) if (d or {}).get("tickets") else [],
        "sample": (d or {}).get("tickets", [])[:2]
    }
    return jsonify(out)


if __name__ == "__main__":
    print("\n  MyPFE Application")
    print(f"  Customer Portal : http://localhost:5000")
    print(f"  NOC Dashboard   : http://localhost:5000/noc")
    print(f"  Debug check     : http://localhost:5000/api/debug\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
