"""Website checks. Each finding carries the evidence that triggered it, so a lead
never claims a problem we didn't actually observe."""
import re
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .fetcher import FetchResult, PoliteFetcher

SOCIAL_HOSTS = ("facebook.com", "fb.com", "instagram.com", "yelp.com", "nextdoor.com", "linktr.ee", "google.com", "business.site", "g.page")
BOOKING_SIGNS = {
    "calendly": "Calendly", "acuityscheduling": "Acuity", "squareup.com/appointments": "Square Appointments",
    "housecallpro": "Housecall Pro", "getjobber": "Jobber", "jobber.com": "Jobber", "servicetitan": "ServiceTitan",
    "setmore": "Setmore", "booksy": "Booksy", "vagaro": "Vagaro", "schedulicity": "Schedulicity",
    "workiz": "Workiz", "fieldedge": "FieldEdge", "simplybook": "SimplyBook", "tidycal": "TidyCal",
    "youcanbook": "YouCanBookMe", "zocdoc": "Zocdoc", "mindbodyonline": "Mindbody", "booking-widget": "booking widget",
    "tekmetric": "Tekmetric", "shopmonkey": "Shopmonkey", "mechanicnet": "MechanicNet", "autoops": "AutoOps",
    "markate": "Markate", "serviceminder": "ServiceMinder", "launch27": "Launch27", "bookingkoala": "BookingKoala",
}
CHAT_SIGNS = {
    "tidio": "Tidio", "intercom": "Intercom", "drift.com": "Drift", "livechatinc": "LiveChat", "tawk.to": "tawk.to",
    "crisp.chat": "Crisp", "hs-scripts": "HubSpot", "zdassets": "Zendesk", "podium": "Podium", "birdeye": "Birdeye",
    "olark": "Olark", "freshchat": "Freshchat", "smartsupp": "Smartsupp", "chatra": "Chatra", "gorgias": "Gorgias",
    "chatbot": "chatbot", "botpress": "Botpress", "manychat": "ManyChat", "leadconnector": "LeadConnector/GHL",
    "ngageics": "Ngage", "apexchat": "ApexChat", "webchat": "web chat",
}
BUILDER_SIGNS = {
    "wix.com": "Wix", "squarespace": "Squarespace", "weebly": "Weebly", "godaddy": "GoDaddy Website Builder",
    "wp-content": "WordPress", "duda": "Duda", "webflow": "Webflow", "shopify": "Shopify", "jimdo": "Jimdo",
}
PARKED_SIGNS = (
    "this domain is for sale", "domain may be for sale", "buy this domain", "parked free", "domain parking",
    "this site can't be reached", "website coming soon", "under construction", "future home of",
    "account suspended", "this account has been suspended", "default web site page", "it works!",
    "index of /", "hostgator.com/?utm", "sedoparking", "parkingcrew", "domain has expired",
)
CONTACT_LINK_RE = re.compile(r"contact|quote|estimate|request|get-started|schedule|appointment|book", re.I)
PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
YEAR_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)


def finding(code: str, title: str, evidence: str, severity: str = "medium", confidence: str = "high") -> dict:
    return {"code": code, "title": title, "evidence": evidence, "severity": severity, "confidence": confidence}


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    return url


def is_social(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)


def _forms(soup: BeautifulSoup) -> list:
    """Forms that look like customer contact/quote forms (not search or newsletter)."""
    out = []
    for f in soup.find_all("form"):
        fields = f.find_all(["input", "textarea", "select"])
        types = {(i.get("type") or "text").lower() for i in fields if i.name == "input"}
        names = " ".join((i.get("name") or "") + " " + (i.get("id") or "") + " " + (i.get("placeholder") or "") for i in fields).lower()
        if f.get("role") == "search" or types <= {"search", "hidden", "submit"}:
            continue
        if f.find("textarea") or "phone" in names or "message" in names or "tel" in types or len(fields) >= 4:
            out.append(f)
    return out


def _embedded_form_providers(html_lower: str) -> list[str]:
    providers = {"jotform": "Jotform", "typeform": "Typeform", "formstack": "Formstack", "hsforms": "HubSpot Forms",
                 "google.com/forms": "Google Forms", "forms.gle": "Google Forms", "wufoo": "Wufoo", "cognitoforms": "Cognito Forms",
                 "123formbuilder": "123FormBuilder", "gravityforms": "Gravity Forms", "wpcf7": "Contact Form 7",
                 "wpforms": "WPForms", "ninja-forms": "Ninja Forms", "elementor-form": "Elementor Forms", "formspree": "Formspree"}
    return sorted({v for k, v in providers.items() if k in html_lower})


def inspect_html(html: str, url: str) -> tuple[list[dict], dict]:
    """Static checks on one page's HTML. Returns (findings, facts)."""
    if html.count("�") > max(20, len(html) // 50):
        return [finding("unreadable_page", "Couldn't read the homepage",
                        "Response body couldn't be decoded as HTML; review the site manually.", "low", "low")], {"unreadable": True}
    soup = BeautifulSoup(html, "html.parser")
    lower = html.lower()
    findings: list[dict] = []
    text = soup.get_text(" ", strip=True)
    words = len(text.split())
    facts: dict = {"word_count": words}
    # Single-page apps ship an empty shell and render in the browser; we don't run
    # JavaScript, so content-based findings on such pages are unreliable.
    js_shell = words < 80 and (
        re.search(r'id=["\'](root|app|__next|__nuxt)["\']|ng-app|data-reactroot', lower) or len(soup.find_all("script")) >= 8
    )
    if js_shell:
        facts["js_rendered"] = True

    # Parked / placeholder / suspended pages.
    hit = next((s for s in PARKED_SIGNS if s in lower), None)
    if hit and words < 400:
        findings.append(finding("parked_or_placeholder", "Website is a placeholder, parked or suspended page",
                                f'Homepage contains "{hit}" and only {words} words of text.', "high"))
        facts["placeholder"] = True
        return findings, facts
    if words < 60 and not soup.find("iframe"):
        findings.append(finding("thin_content", "Homepage has almost no content",
                                f"Only {words} words of visible text on the homepage.", "medium", "medium"))

    builders = sorted({v for k, v in BUILDER_SIGNS.items() if k in lower})
    gen = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    generator = (gen.get("content") or "").strip() if gen else ""
    facts["platform"] = generator or (", ".join(builders) if builders else None)

    # Mobile usability.
    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    if not viewport:
        findings.append(finding("no_viewport", "Not mobile-friendly",
                                "No <meta name=\"viewport\"> tag, so phones render the desktop layout zoomed out.", "high"))
    elif "width=device-width" not in (viewport.get("content") or "").lower():
        findings.append(finding("bad_viewport", "Mobile viewport misconfigured",
                                f'Viewport tag is "{viewport.get("content")}" (missing width=device-width).', "medium"))

    # Outdated technology.
    if soup.find(["frameset", "frame"]):
        findings.append(finding("frames", "Uses HTML frames (1990s technique)", "Page is built with <frameset>/<frame>.", "high"))
    if soup.find("embed", attrs={"src": re.compile(r"\.swf", re.I)}) or ".swf" in lower:
        findings.append(finding("flash", "Relies on Flash, which no browser supports", "Page references a .swf Flash file.", "high"))
    if soup.find(["font", "center", "marquee"]):
        tag = soup.find(["font", "center", "marquee"]).name
        findings.append(finding("legacy_markup", "Outdated HTML markup", f"Uses the obsolete <{tag}> tag.", "low", "medium"))
    jq = re.search(r"jquery[.-]?(1\.(?:[0-9]|1[01])\.\d+)(?:\.min)?\.js", lower)
    if jq:
        findings.append(finding("old_jquery", "Outdated JavaScript libraries", f"Loads jQuery {jq.group(1)} (released 2016 or earlier).", "low"))
    wp = re.match(r"wordpress\s+(\d+)\.(\d+)", generator.lower())
    if wp and (int(wp.group(1)), int(wp.group(2))) < (6, 0):
        findings.append(finding("old_cms", "Outdated, unpatched CMS",
                                f'Generator tag reports "{generator}"; WordPress versions before 6.0 no longer get security fixes.', "high"))
    if re.search(r"frontpage|dreamweaver|iweb|microsoft word", generator.lower()):
        findings.append(finding("old_builder", "Built with discontinued software", f'Generator tag: "{generator}".', "medium"))

    years = [int(y) for y in YEAR_RE.findall(html)]
    if years:
        latest = max(years)
        facts["copyright_year"] = latest
        if latest <= date.today().year - 3:
            findings.append(finding("stale_copyright", "Site appears unmaintained",
                                    f"Footer copyright year is {latest}.", "medium", "medium"))

    # SEO basics.
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    facts["title"] = title
    if not title:
        findings.append(finding("no_title", "Missing page title", "The homepage has no <title>, which is what Google shows as the headline.", "medium"))
    elif len(title) < 15 or title.lower() in ("home", "homepage", "welcome", "untitled"):
        findings.append(finding("weak_title", "Weak page title", f'Homepage title is just "{title}".', "low"))
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if not desc or not (desc.get("content") or "").strip():
        findings.append(finding("no_meta_description", "Missing meta description", "No meta description, so search results show random page text.", "low"))
    if not soup.find("h1"):
        findings.append(finding("no_h1", "No main heading (H1)", "Homepage has no <h1> heading.", "low"))
    robots_meta = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    if robots_meta and "noindex" in (robots_meta.get("content") or "").lower():
        findings.append(finding("noindex", "Homepage is hidden from Google",
                                f'<meta name="robots" content="{robots_meta.get("content")}"> tells search engines not to index it.', "high"))
    imgs = soup.find_all("img")
    no_alt = [i for i in imgs if not (i.get("alt") or "").strip()]
    if len(imgs) >= 5 and len(no_alt) / len(imgs) > 0.5:
        findings.append(finding("img_alt", "Images missing alt text", f"{len(no_alt)} of {len(imgs)} images have no alt text (SEO + accessibility).", "low"))
    if not soup.find("script", attrs={"type": "application/ld+json"}) and "itemtype" not in lower:
        findings.append(finding("no_schema", "No LocalBusiness structured data", "No schema.org markup, which helps Google show hours, reviews and location.", "low", "medium"))

    # Contact / lead capture.
    forms = _forms(soup)
    providers = _embedded_form_providers(lower)
    facts["has_form"] = bool(forms or providers)
    facts["form_providers"] = providers
    for f in forms:
        action = (f.get("action") or "").strip().lower()
        if action.startswith("mailto:"):
            findings.append(finding("mailto_form", "Contact form relies on the visitor's email app",
                                    f"Form action is {action[:60]}, which fails for most visitors without a desktop mail client.", "high"))
        elif url.startswith("https") and action.startswith("http://"):
            findings.append(finding("insecure_form", "Form submits over an insecure connection",
                                    f"Form posts to {action[:80]} (plain HTTP) from an HTTPS page.", "high"))
    tel_links = soup.find_all("a", href=re.compile(r"^tel:", re.I))
    phones_in_text = PHONE_RE.findall(text)
    facts["has_click_to_call"] = bool(tel_links)
    if phones_in_text and not tel_links:
        findings.append(finding("no_click_to_call", "Phone number isn't tappable on mobile",
                                f"Shows {phones_in_text[0]} but has no tel: link.", "medium"))
    emails = {m.group(0) for m in EMAIL_RE.finditer(" ".join(a.get("href", "") for a in soup.find_all("a", href=re.compile("^mailto:", re.I))))}
    facts["emails"] = sorted(e.lower() for e in emails if not e.lower().endswith((".png", ".jpg", "example.com", "sentry.io", "wixpress.com", "mysite.com", "domain.com", "yourdomain.com", "email.com")))

    booking = sorted({v for k, v in BOOKING_SIGNS.items() if k in lower})
    facts["booking"] = booking
    chat = sorted({v for k, v in CHAT_SIGNS.items() if k in lower})
    facts["chat"] = chat

    facts["contact_links"] = sorted({
        urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
        if CONTACT_LINK_RE.search(a["href"] + " " + a.get_text(" ", strip=True))
        and not a["href"].lower().startswith(("tel:", "mailto:", "javascript:", "#"))
        and urlparse(urljoin(url, a["href"])).netloc == urlparse(url).netloc
    })[:3]
    if js_shell:
        findings = [f for f in findings if f["code"] != "thin_content"]
        for f in findings:
            if f["code"] in JS_SENSITIVE:
                f["confidence"] = "low"
                f["evidence"] += " (Page renders with JavaScript; verify in a browser.)"
    return findings, facts


# Findings that depend on rendered page content, which JS-rendered sites don't expose to us.
JS_SENSITIVE = {"no_title", "weak_title", "no_meta_description", "no_h1", "img_alt", "no_schema",
                "stale_copyright", "no_click_to_call", "no_contact_form", "no_booking", "no_chat"}


def website_error_finding(res: FetchResult) -> dict:
    labels = {
        "dns": ("Website domain doesn't resolve", "high"),
        "ssl": ("Website shows a security (SSL certificate) error", "high"),
        "timeout": ("Website doesn't load", "high"),
        "connect": ("Website server refuses connections", "high"),
        "redirect_loop": ("Website is stuck in a redirect loop", "high"),
        "http_error": ("Website returns an error page", "high"),
        "other": ("Website couldn't be loaded", "medium"),
    }
    title, sev = labels.get(res.error or "other", labels["other"])
    if res.error == "http_error" and res.status in (401, 403, 429, 503):
        # Often bot protection rather than a broken site: don't overclaim.
        return finding("access_restricted", "Website blocked our automated check",
                       f"{res.url} answered HTTP {res.status}. May be bot protection; verify in a browser.", "low", "low")
    if res.error == "dns":
        return finding("site_dns", title, f"{res.url} no longer resolves (the domain may have lapsed). "
                       "They may have moved to a new domain; check Google before pitching.", sev, "medium")
    return finding(f"site_{res.error or 'error'}", title, f"{res.url}: {res.error_detail}", sev)


async def analyze_site(url: str, fetcher: PoliteFetcher) -> dict:
    """Fetch the homepage (+ a contact page if needed) and run all checks."""
    url = normalize_url(url)
    findings: list[dict] = []
    res = await fetcher.get(url)

    # http:// that fails can still work on https://, and vice versa.
    if not res.ok and res.error in ("connect", "timeout", "ssl", "dns", "other"):
        alt = url.replace("http://", "https://", 1) if url.startswith("http://") else url.replace("https://", "http://", 1)
        alt_res = await fetcher.get(alt)
        if alt_res.ok:
            if res.error == "ssl":
                findings.append(website_error_finding(res))
            res = alt_res

    if res.error == "blocked_by_robots":
        return {"website_status": "robots_blocked", "final_url": url, "findings": [
            finding("robots_blocked", "Site owner disallows automated analysis",
                    "robots.txt disallows our crawler, so we skipped content checks. Review manually.", "low", "low")], "facts": {}}
    if not res.ok:
        f = website_error_finding(res)
        status = "restricted" if f["code"] == "access_restricted" else "down"
        return {"website_status": status, "final_url": res.final_url or url, "findings": [f], "facts": {}}

    final = res.final_url or url
    facts: dict = {"response_seconds": round(res.elapsed, 2), "final_url": final}
    if urlparse(final).netloc.removeprefix("www.") != urlparse(url).netloc.removeprefix("www."):
        facts["redirected_to"] = final
        if is_social(final):
            return {"website_status": "social_only", "final_url": final, "facts": facts, "findings": [
                finding("social_only", "No real website (domain redirects to a social profile)",
                        f"{url} redirects to {final}.", "high")]}

    if final.startswith("http://"):
        https = await fetcher.get(final.replace("http://", "https://", 1), check_robots=False)
        if not https.ok:
            findings.append(finding("no_https", "No secure HTTPS version",
                                    f"Site only loads over http://; browsers label it \"Not secure\" ({https.error_detail or https.error}).", "high"))
        else:
            findings.append(finding("no_https_redirect", "Doesn't force HTTPS",
                                    "HTTPS works but the site doesn't redirect http:// visitors to it.", "low"))
    if res.elapsed > 4:
        findings.append(finding("slow", "Slow to load", f"Homepage HTML took {res.elapsed:.1f}s to download.", "medium"))

    page_findings, page_facts = inspect_html(res.html, final)
    findings += page_findings
    facts.update(page_facts)
    if page_facts.get("placeholder"):
        return {"website_status": "placeholder", "final_url": final, "findings": findings, "facts": facts}
    if page_facts.get("unreadable"):
        return {"website_status": "restricted", "final_url": final, "findings": findings, "facts": facts}

    # No form on the homepage: check the most likely contact page before claiming it's missing.
    if not facts.get("has_form"):
        checked = []
        for link in facts.get("contact_links", [])[:2]:
            sub = await fetcher.get(link)
            checked.append(link)
            if sub.ok:
                sub_findings, sub_facts = inspect_html(sub.html, sub.final_url or link)
                findings += [f for f in sub_findings if f["code"] in ("mailto_form", "insecure_form")]
                facts["emails"] = sorted(set(facts.get("emails", [])) | set(sub_facts.get("emails", [])))
                facts["booking"] = sorted(set(facts["booking"]) | set(sub_facts["booking"]))
                facts["chat"] = sorted(set(facts["chat"]) | set(sub_facts["chat"]))
                if sub_facts.get("has_form"):
                    facts["has_form"] = True
                    break
        if not facts.get("has_form"):
            where = "the homepage" + (f" or {', '.join(urlparse(c).path or '/' for c in checked)}" if checked else " (no contact page link found)")
            findings.append(finding("no_contact_form", "No online contact or quote form",
                                    f"No customer inquiry form found on {where}.", "high" if not checked else "medium",
                                    "high" if checked else "medium"))

    if not facts.get("booking"):
        findings.append(finding("no_booking", "No online booking or scheduling",
                                "No scheduling tool (Calendly, Housecall Pro, Jobber, ServiceTitan, etc.) detected.", "medium", "medium"))
    if not facts.get("chat"):
        findings.append(finding("no_chat", "No chat or instant-response option",
                                "No chat widget or messaging assistant detected on the site.", "low", "medium"))

    base = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
    sitemap = await fetcher.head_ok(base + "/sitemap.xml")
    if sitemap is not None and sitemap >= 400:
        rp = fetcher._robots.get(base)
        if not (rp and rp.site_maps()):
            findings.append(finding("no_sitemap", "No XML sitemap", f"{base}/sitemap.xml returns HTTP {sitemap}.", "low"))

    if facts.get("js_rendered"):
        for f in findings:
            if f["code"] in JS_SENSITIVE and f["confidence"] != "low":
                f["confidence"] = "low"
                f["evidence"] += " (Page renders with JavaScript; verify in a browser.)"
    return {"website_status": "ok", "final_url": final, "findings": findings, "facts": facts}
