"""Cold email drafts built from a lead's evidence: free, offline, copy-and-paste ready.

Rules the drafts follow (same as the rest of the app):
  - Only observed facts. Verified findings are stated plainly; probable ones are hedged
    ("it looked like"); unverified ones and optional features are never claimed.
  - No invented urgency or numbers, no claims of lost customers or revenue.
  - An easy way to opt out ("reply no thanks"). The email ends at "Thanks," so your own
    email signature follows; CAN-SPAM expects a postal address in commercial email, so keep
    one in that signature. Nothing here sends email.
"""
import re
from urllib.parse import quote, urlparse

from .findings import confidence_level, enrich

VARIANTS = {"short": "Short first email", "detailed": "Detailed first email", "followup": "Follow-up (3–5 days later)"}
PLACEHOLDER = {"name": "[Your name]", "company": "[Your company]"}


def _domain(lead: dict) -> str | None:
    url = lead.get("final_url") or lead.get("website")
    if not url:
        return None
    host = urlparse(url if "://" in url else "http://" + url).netloc
    return host.removeprefix("www.") or None


def _usable(f: dict | None) -> bool:
    """A finding we may mention: a real problem (not an optional feature) that's at least probable."""
    return bool(f) and f.get("tier") in ("strong", "moderate", "weak") and \
        f.get("confidence_level", confidence_level(f)) in ("verified", "probable") and bool(f.get("talking_point"))






# How to say "we help with X" for each offering (falls back to a generic phrase).
HELPS_WITH = {
    "new business website": "building websites that bring in customers",
    "fix broken or insecure website": "fixing website problems",
    "contact and quote-request path": "making it easy for customers to get in touch online",
    "mobile-friendly rebuild": "mobile-friendly websites",
    "technical seo fixes": "getting found on Google",
    "online booking & scheduling": "online booking",
    "ai chat assistant": "AI chat assistants for their websites",
}




# Teasers: say enough that the email is clearly based on something real, but keep the
# specifics for the conversation. (noun phrase, short area name)
_RELIABILITY = ("some trouble with your site loading", "site reliability")
_SECURITY = ("a security setting that causes browsers to flag your site", "site security")
_CONTACT = ("a few spots where it's harder than it should be for customers to reach you", "how customers reach you")
_MOBILE = ("a few issues with how the site works on phones", "the mobile experience")
_SEO = ("a few things holding the site back on Google", "Google visibility")
_OWN_SITE = ("an opportunity to give the business a website of its own", "your web presence")
HINTS = {
    **dict.fromkeys(["site_timeout", "site_connect", "site_http_error", "site_redirect_loop", "site_other", "site_intermittent"], _RELIABILITY),
    "site_dns": ("an issue with the web address listed for your business", "your web address"),
    **dict.fromkeys(["site_ssl", "no_https", "insecure_form", "no_https_redirect"], _SECURITY),
    "old_cms": ("some outdated software behind your site", "site security"),
    **dict.fromkeys(["broken_important_link", "broken_links"],
                    ("a few things on your site that aren't working the way a customer would expect", "broken pages")),
    **dict.fromkeys(["no_contact_path", "no_contact_form", "mailto_form", "no_click_to_call", "claimed_feature_missing"], _CONTACT),
    "phone_mismatch": ("some inconsistent contact details between your site and your listings", "your contact details"),
    **dict.fromkeys(["no_viewport", "bad_viewport", "frames", "flash"], _MOBILE),
    "slow": ("a few things slowing the site down", "site speed"),
    "thin_content": ("some gaps in what the site tells visitors about your services", "your site's content"),
    "noindex": ("a setting that's affecting how your site shows up on Google", "Google visibility"),
    **dict.fromkeys(["no_title", "weak_title", "no_meta_description", "no_h1", "old_builder", "stale_copyright"], _SEO),
    "parked_or_placeholder": ("an issue with what visitors see at your web address", "your website"),
    **dict.fromkeys(["social_only", "no_website_confirmed"], _OWN_SITE),
}


def _hint(f: dict) -> tuple[str, str]:
    return HINTS.get(f["code"], ("a few things on your website worth a look", "your website"))


def _noticed(f: dict) -> str:
    """'noticed X' for verified findings, 'noticed what looks like X' for probable ones."""
    phrase, _ = _hint(f)
    probable = f.get("confidence_level", confidence_level(f)) == "probable"
    return f"noticed {'what looks like ' if probable else ''}{phrase}"


CTA = "I'd be happy to walk you through what I found; it only takes a few minutes. Would you be open to a quick call this week?"
OPT_OUT = "If you'd rather not hear from me, just reply \"no thanks\" and I won't email again."


def draft(lead: dict, sender: dict, variant: str = "short") -> dict:
    """Build subject options and a body for one lead. `lead` is the API's public lead
    (enriched findings + explanation)."""
    variant = variant if variant in VARIANTS else "short"
    # Best-ranked person from Sunbiz; a name+city-only ("low") match is never used to greet someone.
    owner = next((o for o in lead.get("owners") or [] if o.get("match_confidence", "high") in ("high", "medium")), None)
    first = owner["name"].split()[0] if owner and owner.get("name") else None
    name = lead["name"]
    domain = _domain(lead)
    ex = lead.get("explanation") or {}
    strongest = ex.get("strongest")
    findings = [f if "tier" in f else enrich(f) for f in lead.get("findings") or []]
    usable = [f for f in findings if _usable(f)]
    main = next((f for f in usable if strongest and f["code"] == strongest.get("code")), usable[0] if usable else None)
    extra = next((f for f in usable if main and f["code"] != main["code"]), None)
    service = (strongest or {}).get("service") or ""
    helps = HELPS_WITH.get(service.lower(), "their websites and online presence")
    warnings = []
    if not main:
        warnings.append("No verified or probable problem to mention, so this email is a general introduction. "
                        "Consider skipping this lead or checking their site first.")
    if owner:
        warnings.append(f"Greeting uses {owner['name']} ({owner['title']}) from Florida Sunbiz records; "
                        "make sure they're still involved with the business.")
    if not lead.get("email"):
        warnings.append("No email address on file. Look for one on their website or contact page before sending.")

    subjects = _subjects(name, domain, main, variant)
    greeting = f"Hi {first}," if first else f"Hi {name} team,"
    sender_name = (sender.get("name") or "").strip() or PLACEHOLDER["name"]
    company = (sender.get("company") or "").strip() or PLACEHOLDER["company"]
    intro = f"I'm {sender_name} with {company}. We help local businesses with {helps}."

    where = domain or "your website"
    if variant == "followup":
        body = [greeting, "",
                "Following up on my note from last week" + (f" about {domain}." if domain else "."),
                (f"I'm still happy to walk you through what I noticed about {_hint(main)[1]}. It only takes a few minutes."
                 if main else "I'd still be glad to share a few quick ideas for your website."),
                "", "Would a quick call this week work?"]
    else:
        body = [greeting, "", intro, ""]
        if main and main["code"] == "new_registration":
            body.append(f"Congratulations on recently getting {name} registered. Most new businesses don't have their "
                        "website and online listings fully set up yet, and that's what we help with.")
        elif main:
            body.append(f"I took a look at {where} and {_noticed(main)}.")
            if variant == "detailed" and extra and _hint(extra) != _hint(main):
                body.append(f"I also {_noticed(extra)}.")
        else:
            body.append("I came across your business and wanted to introduce myself in case you're looking for help online.")
        body += ["", CTA if main else "Would you be open to a quick call this week?"]
    # Ends at "Thanks," so the sender's own email signature follows.
    body += ["", OPT_OUT, "", "Thanks,"]
    text = "\n".join(body).replace("\n\n\n", "\n\n")

    verify = [f"Before sending: {main['verify']}"] if main and main.get("verify") else []
    if main:
        # The email only hints; this is what to have ready for the call.
        verify.insert(0, f"Specifics to share on the call: {main['title']}: {main.get('evidence', '')}")
    return {"variant": variant, "variants": VARIANTS, "subjects": subjects, "subject": subjects[0], "body": text,
            "to": lead.get("email"), "warnings": warnings, "verify": verify,
            "mailto": (f"mailto:{lead['email']}?subject={quote(subjects[0])}&body={quote(text)}" if lead.get("email") else None),
            "based_on": main["title"] if main else None}


def _subjects(name: str, domain: str | None, main: dict | None, variant: str) -> list[str]:
    if variant == "followup":
        return [f"Re: {domain or name}", f"Following up, {name}", "Quick follow-up"]
    area = _hint(main)[1] if main and main["code"] != "new_registration" else None
    out = [f"Quick note about {domain or name}"]
    if area:
        out.append(f"A question about {area}" + (f" on {domain}" if domain else ""))
    out.append(f"Idea for {name}")
    return out[:3]



