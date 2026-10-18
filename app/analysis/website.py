"""Website checks. Each finding carries the evidence that triggered it, so a lead
never claims a problem we didn't actually observe."""
import asyncio
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


# Bump when checks change so older audits get re-run (see db.unanalyzed).
AUDIT_VERSION = 2
LINK_CHECK_LIMIT = 8      # internal links checked per site (politeness: ~2 s apart)
RETRY_DELAY = 5.0         # seconds before re-trying a site that failed to load
# Calls to action that promise an online way to book or get a quote.
CTA_RE = re.compile(r"\b(book (?:online|now|an appointment)|schedule (?:online|now|service|an appointment)|online (?:booking|scheduling)"
                    r"|request (?:a |an )?(?:quote|estimate|appointment)|get (?:a |your )?free (?:quote|estimate)|free (?:quote|estimate)s?)\b", re.I)
# Only explicit *online* promises count as a claim we can check against the site; "call to
# request a quote" is a valid phone path, not a mismatch.
CTA_ONLINE_RE = re.compile(r"\b(book(?:ing)? online|schedule (?:service |an appointment )?online|online (?:booking|scheduling|quotes?|estimates?)"
                           r"|(?:request|get) (?:a |an |your )?(?:free )?(?:quote|estimate) online)\b", re.I)
IMPORTANT_LINK_RE = re.compile(r"contact|quote|estimate|book|schedule|appointment|request|service", re.I)
SKIP_LINK_RE = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|mp3)(\?|$)|/wp-json|/feed/?$|/cdn-cgi/", re.I)


def finding(code: str, title: str, evidence: str, severity: str = "medium", confidence: str = "high",
            page: str | None = None, details: dict | None = None) -> dict:
    """An observation. `confidence` is how sure we are of what the title claims
    (high=verified, medium=probable, low=unverified); `page` is the affected URL."""
    f = {"code": code, "title": title, "evidence": evidence, "severity": severity, "confidence": confidence}
    if page:
        f["page"] = page
    if details:
        f["details"] = details
    return f


def phones_in(text: str, soup: BeautifulSoup | None = None) -> set[str]:
    """10-digit phone numbers shown in page text or tel: links."""
    from ..dedup import normalize_phone
    raw = PHONE_RE.findall(text)
    if soup is not None:
        raw += [a["href"][4:] for a in soup.find_all("a", href=re.compile(r"^tel:", re.I))]
    return {p for p in (normalize_phone(x) for x in raw) if p}


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


def _internal_links(soup: BeautifulSoup, url: str) -> list[dict]:
    """Same-site links from a page, contact/quote/booking pages first."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    seen, out = {urlparse(url).path.rstrip("/") or "/"}, []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith(("tel:", "mailto:", "javascript:", "#", "sms:", "data:")):
            continue
        absolute = urljoin(url, href).split("#")[0]
        p = urlparse(absolute)
        if p.scheme not in ("http", "https") or p.netloc.lower().removeprefix("www.") != host or SKIP_LINK_RE.search(absolute):
            continue
        key = p.path.rstrip("/") or "/"
        if key in seen:
            continue
        seen.add(key)
        label = a.get_text(" ", strip=True)[:40] or key
        out.append({"url": absolute, "label": label, "important": bool(IMPORTANT_LINK_RE.search(f"{href} {label}"))})
    out.sort(key=lambda l: not l["important"])
    return out[:40]


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
                                    f"Form action is {action[:60]}, so submitting opens the visitor's email program instead of sending. (Form not submitted.)", "high", page=url))
        elif url.startswith("https") and action.startswith("http://"):
            findings.append(finding("insecure_form", "Form submits over an insecure connection",
                                    f"Form posts to {action[:80]} (plain HTTP) from an HTTPS page. (Form not submitted.)", "high", page=url))
    if forms:
        facts["forms_found"] = facts.get("forms_found", []) + [url]
    tel_links = soup.find_all("a", href=re.compile(r"^tel:", re.I))
    phones_in_text = PHONE_RE.findall(text)
    facts["has_click_to_call"] = bool(tel_links)
    facts["site_phones"] = sorted(phones_in(text, soup))
    if phones_in_text and not tel_links:
        findings.append(finding("no_click_to_call", "Phone number isn't tappable on mobile",
                                f"Shows {phones_in_text[0]} but has no tel: link.", "medium", page=url))
    emails = {m.group(0) for m in EMAIL_RE.finditer(" ".join(a.get("href", "") for a in soup.find_all("a", href=re.compile("^mailto:", re.I))))}
    facts["emails"] = sorted(e.lower() for e in emails if not e.lower().endswith((".png", ".jpg", "example.com", "sentry.io", "wixpress.com", "mysite.com", "domain.com", "yourdomain.com", "email.com")))
    # Visible text addresses count as a contact path too (some sites don't link them).
    facts["has_email_text"] = bool(facts["emails"]) or bool(EMAIL_RE.search(text))

    # Calls to action that promise online booking/quotes, and whether one is clickable.
    claim = CTA_ONLINE_RE.search(text)
    facts["cta_claim"] = claim.group(0) if claim else None
    facts["cta_links"] = sorted({
        urljoin(url, el.get("href") or "") for el in soup.find_all(["a", "button"])
        if CTA_RE.search(el.get_text(" ", strip=True)) or CTA_RE.search((el.get("href") or "").replace("-", " "))
        if (el.get("href") or "").strip() not in ("", "#") or el.name == "button"
    })[:5]
    facts["internal_links"] = _internal_links(soup, url)

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
                "stale_copyright", "no_click_to_call", "no_contact_form", "no_booking", "no_chat",
                "no_contact_path", "claimed_feature_missing", "phone_mismatch"}


def website_error_finding(res: FetchResult, attempts: int = 1) -> dict:
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
                       f"{res.url} answered HTTP {res.status}. May be bot protection; verify in a browser.", "low", "low", page=res.url)
    tries = f"Failed on {attempts} attempts {RETRY_DELAY:.0f}s apart" if attempts > 1 else "Failed once"
    # Only a failure we saw more than once counts as verified.
    confidence = "high" if attempts > 1 and res.error != "other" else "medium"
    if res.error == "dns":
        return finding("site_dns", title, f"{tries}: {res.url} doesn't resolve (the domain may have lapsed or the business may have moved).",
                       sev, confidence, page=res.url)
    return finding(f"site_{res.error or 'error'}", title, f"{tries}: {res.error_detail}", sev, confidence, page=res.url)


async def _fetch(url: str, fetcher: PoliteFetcher, findings: list[dict]) -> FetchResult:
    """Fetch, falling back between http:// and https:// (sites often work on only one)."""
    res = await fetcher.get(url)
    if not res.ok and res.error in ("connect", "timeout", "ssl", "dns", "other"):
        alt = url.replace("http://", "https://", 1) if url.startswith("http://") else url.replace("https://", "http://", 1)
        alt_res = await fetcher.get(alt)
        if alt_res.ok:
            if res.error == "ssl":
                f = website_error_finding(res)
                f["confidence"] = "high"  # the certificate error itself is a deterministic observation
                f["evidence"] = f"https:// fails with a certificate error ({res.error_detail}); the http:// version loads."
                findings.append(f)
            return alt_res
    return res


async def _check_links(links: list[dict], fetcher: PoliteFetcher, known: dict[str, int]) -> tuple[list, list, dict]:
    """Check internal links politely. Returns (broken_important, broken_other, stats)."""
    broken_imp, broken, stats = [], [], {"checked": 0, "unchecked": []}
    for link in links[:LINK_CHECK_LIMIT]:
        status = known.get(link["url"])
        if status is None:
            status, err = await fetcher.link_status(link["url"])
            if status is not None and status >= 500:  # could be a blip: confirm once
                status, err = await fetcher.link_status(link["url"])
            if status is None:
                stats["unchecked"].append(f"{link['url']} ({err})")
                continue
        stats["checked"] += 1
        if status in (404, 410) or status >= 500:
            item = {**link, "status": status, "confidence": "high" if status in (404, 410) else "medium"}
            (broken_imp if link["important"] else broken).append(item)
    return broken_imp, broken, stats


async def analyze_site(url: str, fetcher: PoliteFetcher, listing_phone: str | None = None) -> dict:
    """Fetch the homepage (+ contact page and a few internal links) and run all checks.
    Never submits forms: forms are inspected statically and flagged as untested."""
    from ..dedup import normalize_phone
    url = normalize_url(url)
    findings: list[dict] = []
    base_facts = {"audit_version": AUDIT_VERSION}
    res = await _fetch(url, fetcher, findings)

    if res.error == "blocked_by_robots":
        return {"website_status": "robots_blocked", "final_url": url, "facts": base_facts, "findings": [
            finding("robots_blocked", "Site owner disallows automated analysis",
                    "robots.txt disallows our crawler, so we skipped content checks. Review manually.", "low", "low", page=url)]}
    if not res.ok:
        first = website_error_finding(res)
        if first["code"] == "access_restricted":
            return {"website_status": "restricted", "final_url": res.final_url or url, "findings": [first], "facts": base_facts}
        # One failure isn't proof: wait and try again before calling the site down.
        await asyncio.sleep(RETRY_DELAY)
        retry = await _fetch(url, fetcher, findings)
        if not retry.ok:
            f = website_error_finding(retry, attempts=2)
            status = "restricted" if f["code"] == "access_restricted" else "down"
            return {"website_status": status, "final_url": retry.final_url or url, "findings": findings + [f], "facts": base_facts}
        findings.append(finding("site_intermittent", "Website failed to load, then worked",
                                f"First attempt: {res.error_detail or res.error}; second attempt {RETRY_DELAY:.0f}s later succeeded.",
                                "medium", "medium", page=url))
        res = retry

    final = res.final_url or url
    facts: dict = {**base_facts, "response_seconds": round(res.elapsed, 2), "final_url": final}
    if urlparse(final).netloc.removeprefix("www.") != urlparse(url).netloc.removeprefix("www."):
        facts["redirected_to"] = final
        if is_social(final):
            return {"website_status": "social_only", "final_url": final, "facts": facts, "findings": [
                finding("social_only", "No real website (domain redirects to a social profile)",
                        f"{url} redirects to {final}.", "high", page=final)]}

    if final.startswith("http://"):
        https = await fetcher.get(final.replace("http://", "https://", 1), check_robots=False)
        if not https.ok:
            findings.append(finding("no_https", "No secure HTTPS version",
                                    f"Site only loads over http://; browsers label it \"Not secure\" ({https.error_detail or https.error}).", "high"))
        else:
            findings.append(finding("no_https_redirect", "Doesn't force HTTPS",
                                    "HTTPS works but the site doesn't redirect http:// visitors to it.", "low"))
    if res.elapsed > 4:
        findings.append(finding("slow", "Slow to load", f"Homepage HTML took {res.elapsed:.1f}s to download (single measurement).", "medium", "medium"))

    page_findings, page_facts = inspect_html(res.html, final)
    findings += page_findings
    facts.update(page_facts)
    if page_facts.get("placeholder"):
        return {"website_status": "placeholder", "final_url": final, "findings": _with_page(findings, final), "facts": facts}
    if page_facts.get("unreadable"):
        return {"website_status": "restricted", "final_url": final, "findings": _with_page(findings, final), "facts": facts}

    known_status: dict[str, int] = {}
    # No form on the homepage: check the most likely contact page before saying it's missing.
    if not facts.get("has_form"):
        checked = []
        for link in facts.get("contact_links", [])[:2]:
            sub = await fetcher.get(link)
            checked.append(link)
            if sub.status:
                known_status[link] = sub.status
            if sub.ok:
                sub_findings, sub_facts = inspect_html(sub.html, sub.final_url or link)
                findings += [f for f in sub_findings if f["code"] in ("mailto_form", "insecure_form")]
                for key in ("emails", "booking", "chat", "site_phones", "forms_found", "cta_links"):
                    facts[key] = sorted(set(facts.get(key) or []) | set(sub_facts.get(key) or []))
                facts["has_email_text"] = facts.get("has_email_text") or sub_facts.get("has_email_text")
                if sub_facts.get("has_form"):
                    facts["has_form"] = True
                    break
        facts["contact_pages_checked"] = checked
        if not facts.get("has_form"):
            where = "the homepage" + (f" or {', '.join(urlparse(c).path or '/' for c in checked)}" if checked else "")
            other_paths = [p for p, present in (("a phone number", facts.get("site_phones")),
                                                ("an email address", facts.get("has_email_text")),
                                                ("a contact page link", facts.get("contact_links"))) if present]
            if other_paths:
                findings.append(finding("no_contact_form", "No online contact or quote form",
                                        f"No inquiry form on {where}; visitors can still use {', '.join(other_paths[:-1]) + ' or ' if len(other_paths) > 1 else ''}{other_paths[-1]}.", "low", "medium"))
            else:
                findings.append(finding("no_contact_path", "No way to contact the business on the website",
                                        f"No phone number, email address, contact form or contact page link found on {where}.", "high", "high"))

    if facts.get("forms_found"):
        findings.append(finding("form_untested", "Contact form found (not tested)",
                                f"Form on {', '.join(facts['forms_found'][:2])}. We inspect forms but never submit them, so whether it works is unknown.",
                                "low", "high", page=facts["forms_found"][0]))

    # Listing phone vs. phones on the site: corroboration, or a possible stale number.
    listed = normalize_phone(listing_phone)
    site_phones = set(facts.get("site_phones") or [])
    facts["contact_corroborated"] = bool(listed and listed in site_phones)
    if listed and site_phones and listed not in site_phones:
        fmt = lambda p: f"({p[:3]}) {p[3:6]}-{p[6:]}"
        shown = sorted(site_phones)
        findings.append(finding("phone_mismatch", "Phone number differs between listing and website",
                                f"Map listing has {fmt(listed)}; website shows {', '.join(fmt(p) for p in shown[:3])}.", "medium", "medium",
                                details={"listing_phone": fmt(listed), "site_phone": fmt(shown[0])}))

    # Site promises online booking/quotes but offers no way to do it.
    if facts.get("cta_claim") and not (facts.get("has_form") or facts.get("booking") or facts.get("cta_links")):
        findings.append(finding("claimed_feature_missing", "Site promises online booking or quotes, but no way to do it",
                                f'Page text says "{facts["cta_claim"]}" but no form, booking tool or matching link/button was found.',
                                "medium", "medium", details={"claim": facts["cta_claim"]}))

    # Broken internal links (important pages first). Only status codes we actually received count.
    broken_imp, broken, stats = await _check_links(facts.pop("internal_links", []), fetcher, known_status)
    facts["links_checked"], facts["links_unchecked"] = stats["checked"], stats["unchecked"]
    if broken_imp:
        b = broken_imp[0]
        findings.append(finding("broken_important_link", f"\"{b['label']}\" link is broken",
                                "; ".join(f'"{x["label"]}" → {x["url"]} returned HTTP {x["status"]}' for x in broken_imp[:3]) + ".",
                                "high", b["confidence"], page=b["url"],
                                details={"link_label": f'"{b["label"]}"'}))
    if broken:
        findings.append(finding("broken_links", f"{len(broken)} broken link{'s' if len(broken) > 1 else ''} on the homepage",
                                "; ".join(f'{x["url"]} returned HTTP {x["status"]}' for x in broken[:4]) + f" ({stats['checked']} links checked).",
                                "medium", "high" if all(x["confidence"] == "high" for x in broken) else "medium", page=broken[0]["url"]))

    if not facts.get("booking"):
        findings.append(finding("no_booking", "No online booking tool detected",
                                "No scheduling tool (Calendly, Housecall Pro, Jobber, ServiceTitan, etc.) detected. Optional feature.", "low", "medium"))
    if not facts.get("chat"):
        findings.append(finding("no_chat", "No chat widget detected",
                                "No chat widget or messaging assistant detected on the site. Optional feature.", "low", "medium"))

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
    return {"website_status": "ok", "final_url": final, "findings": _with_page(findings, final), "facts": facts}


def _with_page(findings: list[dict], page: str) -> list[dict]:
    for f in findings:
        f.setdefault("page", page)
    return findings
