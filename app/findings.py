"""What each audit finding means, separated from what was observed.

A stored finding records the *observation*: code, title, evidence, the affected page,
and how sure we are of it (high/medium/low, shown as verified/probable/unverified).
This catalog adds the *interpretation*: how strong a sales signal it is, why it may
matter, what a person should check by hand, and a factual talking point. Keeping the
interpretation here (not in stored rows) means wording and weights can improve
without re-auditing every site.

Signal strength (tier):
  strong   - a verified, consequential problem (site down, broken contact page, cert error)
  moderate - a real but less severe problem, or a strong one we could only partly confirm
  weak     - minor issues; worth mentioning, not worth a call on their own
  optional - an absent nice-to-have (chat, booking widget, schema markup). Never proof of a
             business problem; scored only as a small tie-breaker.
  none     - not a problem: couldn't check, or neutral context
"""

CONFIDENCE_LEVEL = {"high": "verified", "medium": "probable", "low": "unverified"}

_DOWN_IMPACT = "Visitors arriving from Google, Maps or a directory see an error instead of the business."
_DOWN_VERIFY = "Open the site in a normal browser and on a phone to confirm real visitors see the same failure."
_DOWN_TALK = "When I opened {page} it didn't load ({evidence_short}). Wanted to flag it in case you weren't aware."

CATALOG: dict[str, dict] = {
    # -- site availability & security
    "site_timeout": {"tier": "strong", "impact": _DOWN_IMPACT, "verify": _DOWN_VERIFY, "talk": _DOWN_TALK},
    "site_connect": {"tier": "strong", "impact": _DOWN_IMPACT, "verify": _DOWN_VERIFY, "talk": _DOWN_TALK},
    "site_http_error": {"tier": "strong", "impact": _DOWN_IMPACT, "verify": _DOWN_VERIFY, "talk": _DOWN_TALK},
    "site_redirect_loop": {"tier": "strong", "impact": _DOWN_IMPACT, "verify": _DOWN_VERIFY, "talk": _DOWN_TALK},
    "site_other": {"tier": "moderate", "impact": _DOWN_IMPACT, "verify": _DOWN_VERIFY, "talk": _DOWN_TALK},
    "site_intermittent": {
        "tier": "moderate",
        "impact": "Some visitors may hit an error depending on when they arrive.",
        "verify": "Load the site a few times over a day to see if failures repeat.",
        "talk": "Your site failed to load on my first try and worked on the second ({evidence_short}).",
    },
    "site_dns": {
        "tier": "strong", "actionability": 0.6,
        "impact": "The website address listed for the business leads nowhere.",
        "verify": "Search for the business: they may have moved to a new domain, in which case only the listing needs updating.",
        "talk": "The website listed for you ({page}) doesn't resolve anymore. Is there a newer site I should be looking at?",
    },
    "site_ssl": {
        "tier": "strong",
        "impact": "Browsers show a full-page security warning before the site, which many visitors won't click past.",
        "verify": "Open the https:// address in Chrome or Safari and confirm the warning appears.",
        "talk": "Opening {page} shows a browser security warning about the site's certificate.",
    },
    "no_https": {
        "tier": "strong",
        "impact": "Browsers label the site \"Not secure\", and anything typed into its forms travels unencrypted.",
        "verify": "Try the https:// version in a browser to confirm it fails.",
        "talk": "Your site only works over plain http://, so browsers mark it \"Not secure\".",
    },
    "insecure_form": {
        "tier": "strong",
        "impact": "Customer details typed into the form are sent unencrypted.",
        "verify": "Inspect the form's action URL in a browser; it was not submitted.",
        "talk": "The contact form on {page} sends to a plain http:// address, so submissions aren't encrypted.",
    },
    "no_https_redirect": {
        "tier": "weak",
        "impact": "Visitors who type the address without https may see the \"Not secure\" version.",
        "verify": "Visit http://<domain> and see whether it switches to https.",
        "talk": "Your site works on https but doesn't redirect visitors who arrive on http://.",
    },
    "slow": {
        "tier": "moderate",
        "impact": "Slow pages lose some visitors on mobile connections before they finish loading.",
        "verify": "One measurement from our server; test with PageSpeed Insights before raising it.",
        "talk": "Your homepage took a while to load when I checked it ({evidence_short}).",
    },
    # -- no usable website
    "no_website_confirmed": {
        "tier": "strong",
        "impact": "People searching for the business find only directory listings.",
        "verify": "Search the business name yourself to confirm there's no site.",
        "talk": "I couldn't find a website for your business, only directory listings.",
    },
    # Pre-v0.4 audits stored this when OSM simply had no website tag. It was never evidence
    # of anything, so it's now treated exactly like website_unknown: shown, never scored.
    "no_website_unverified": {
        "tier": "none", "impact": None,
        "verify": "Only the map listing lacks a website. Search for the business before assuming they don't have one.",
        "talk": None,
    },
    "website_unknown": {
        "tier": "none", "impact": None,
        "verify": "No source listed a website and no search ran. Search the business name and phone yourself.",
        "talk": None,
    },
    "website_match_uncertain": {
        "tier": "none", "impact": None,
        "verify": "Open the candidate site(s) and check whether they show this business's phone or address; "
                  "use Edit website to confirm the right one.",
        "talk": None,
    },
    "website_conflict": {
        "tier": "none", "impact": None,
        "verify": "Sources list different websites. Check which one is current before mentioning either.",
        "talk": None,
    },
    "website_identity_unconfirmed": {
        "tier": "none", "impact": None,
        "verify": "The site exists, but its pages didn't show this business's phone or address. Confirm it's theirs "
                  "before raising problems with it.",
        "talk": None,
    },
    "new_registration": {
        "tier": "weak", "actionability": 0.8,
        "impact": "A recently registered business may not have a website or online presence set up yet.",
        "verify": "A state registration shows the legal entity exists, not that it's operating. Look for signs it has "
                  "opened (listing, phone, signage) before reaching out.",
        "talk": "I saw your business was recently registered with the state. Congratulations on the launch.",
    },
    "social_only": {
        "tier": "strong",
        "impact": "Their only web presence is a social profile, which they don't control the layout or reach of.",
        "verify": "Check whether a separate website exists that the listing doesn't mention.",
        "talk": "The website listed for you goes to a social media page ({page}).",
    },
    "parked_or_placeholder": {
        "tier": "strong",
        "impact": "Visitors to the domain see a placeholder or parked page instead of the business.",
        "verify": "Open the domain in a browser to confirm it still shows the placeholder.",
        "talk": "Your domain {page} currently shows a placeholder page rather than your site.",
    },
    "thin_content": {
        "tier": "moderate",
        "impact": "The homepage gives visitors almost no information about services.",
        "verify": "Look at the page in a browser; some content may load with JavaScript.",
        "talk": "Your homepage has very little text about what you offer.",
    },
    # -- broken pages & links
    "broken_important_link": {
        "tier": "strong",
        "impact": "A key page (contact, quote or booking) linked from the homepage is broken, so visitors trying to act hit an error.",
        "verify": "Click the link from the homepage in a browser to confirm the error.",
        "talk": "The {link_label} link on your homepage goes to {page}, which returns an error ({evidence_short}).",
    },
    "broken_links": {
        "tier": "moderate",
        "impact": "Visitors clicking these links from the homepage hit error pages.",
        "verify": "Click the listed links in a browser to confirm.",
        "talk": "A few links on your homepage lead to error pages, for example {page}.",
    },
    # -- contact & lead capture
    "no_contact_path": {
        "tier": "strong",
        "impact": "Visitors have no visible way to reach the business from the site: no phone, email, form or contact page.",
        "verify": "Scroll the homepage on a phone; contact details may be in an image or loaded by JavaScript.",
        "talk": "I couldn't find a phone number, email or contact form on your website.",
    },
    "phone_mismatch": {
        "tier": "moderate",
        "impact": "Customers may call an outdated number, depending on where they found the business.",
        "verify": "We don't know which number is current. Call or check Google Business Profile before mentioning it.",
        "talk": "The phone number on your website, {site_phone}, is different from the one in your map listing, {listing_phone}.",
    },
    "claimed_feature_missing": {
        "tier": "moderate",
        "impact": "The site invites visitors to book or request a quote online, but we found no working way to do it.",
        "verify": "The path may be a JavaScript widget we can't see. Try it in a browser.",
        "talk": "Your site mentions \"{claim}\", but I couldn't find where to do that online.",
    },
    "mailto_form": {
        "tier": "moderate",
        "impact": "The form opens the visitor's email program instead of sending, which often fails on phones and webmail.",
        "verify": "The form was not submitted. Try it on a phone to see what happens.",
        "talk": "The contact form on {page} hands off to an email app instead of sending directly.",
    },
    "no_contact_form": {
        "tier": "weak",
        "impact": "Visitors can reach them in other ways, but can't send a quote request from the site.",
        "verify": "Check for a form loaded by JavaScript or on a page we didn't visit.",
        "talk": "I didn't see a way to request a quote online on your site.",
    },
    "no_click_to_call": {
        "tier": "moderate",
        "impact": "Phone visitors have to copy the number by hand to call.",
        "verify": "Tap the number on a phone to confirm it isn't a link.",
        "talk": "The phone number on your site isn't tappable on mobile.",
    },
    # -- mobile & technology
    "no_viewport": {
        "tier": "moderate",
        "impact": "Phones render the desktop layout zoomed out, making text, buttons and forms hard to use.",
        "verify": "Open the site on a phone to see how much it obstructs calling or requesting a quote.",
        "talk": "On a phone your site shows the shrunken desktop layout rather than a mobile version.",
    },
    "bad_viewport": {
        "tier": "weak",
        "impact": "The mobile layout may not fit the screen properly.",
        "verify": "Check the site on a phone.",
        "talk": "Your site's mobile layout setting looks misconfigured.",
    },
    "frames": {"tier": "moderate", "impact": "Framed sites break on phones and in search results.",
               "verify": "Open on a phone.", "talk": "Your site is built with HTML frames, which don't work well on phones."},
    "flash": {"tier": "moderate", "impact": "Flash content shows as blank in every current browser.",
              "verify": "Check the page in a browser for empty areas.", "talk": "Part of your site uses Flash, which browsers no longer display."},
    "old_cms": {"tier": "moderate", "impact": "WordPress versions this old no longer receive security patches.",
                "verify": "The version comes from the generator tag, which can be stale; confirm with the site owner.",
                "talk": "Your site reports an old WordPress version that no longer gets security updates."},
    "old_builder": {"tier": "weak", "impact": "Built with discontinued software, so it's hard to update.",
                    "verify": "The generator tag can be left over from an old version.", "talk": "Your site appears to have been built with {evidence_short}."},
    "stale_copyright": {"tier": "weak", "impact": "May suggest the site isn't maintained.",
                        "verify": "An old footer year alone doesn't mean the content is out of date.", "talk": None},
    "noindex": {
        "tier": "strong",
        "impact": "The homepage tells search engines not to list it, so it can't appear in Google results.",
        "verify": "Search Google for the domain (site:domain) to see whether it's indexed.",
        "talk": "Your homepage has a setting that tells Google not to list it.",
    },
    # -- optional features & SEO hygiene: never evidence of a problem by themselves
    "no_booking": {"tier": "optional", "impact": "Only matters if they take appointments and want online scheduling.",
                   "verify": "Ask whether customers want to book online before suggesting it.", "talk": None},
    "no_chat": {"tier": "optional", "impact": "Only matters if they get many repeat questions.",
                "verify": "Don't pitch a chatbot without evidence of inquiry volume.", "talk": None},
    "no_schema": {"tier": "optional", "impact": "Structured data can help search listings show details.", "verify": None, "talk": None},
    "no_sitemap": {"tier": "optional", "impact": "Small sites are usually indexed fine without one.", "verify": None, "talk": None},
    "img_alt": {"tier": "optional", "impact": "Affects accessibility and image search.", "verify": None, "talk": None},
    "no_meta_description": {"tier": "optional", "impact": "Search results show page text instead of a summary.", "verify": None, "talk": None},
    "weak_title": {"tier": "optional", "impact": "A generic title says little in search results.", "verify": None, "talk": None},
    "no_title": {"tier": "weak", "impact": "Search results have no proper headline for the site.", "verify": "Check the browser tab title.", "talk": None},
    "no_h1": {"tier": "optional", "impact": "Minor SEO hygiene.", "verify": None, "talk": None},
    "old_jquery": {"tier": "optional", "impact": "Old library; rarely visible to customers.", "verify": None, "talk": None},
    "legacy_markup": {"tier": "optional", "impact": "Old markup; rarely visible to customers.", "verify": None, "talk": None},
    # -- not problems
    "access_restricted": {"tier": "none", "impact": None, "verify": "Bot protection blocked our check. Review the site in a browser.", "talk": None},
    "robots_blocked": {"tier": "none", "impact": None, "verify": "The site asks crawlers not to visit. Review it manually.", "talk": None},
    "unreadable_page": {"tier": "none", "impact": None, "verify": "We couldn't read the page. Review it manually.", "talk": None},
    "analysis_error": {"tier": "none", "impact": None, "verify": "Automated analysis failed. Review manually.", "talk": None},
    "website_found_by_search": {"tier": "none", "impact": None, "verify": "Confirm the search match is really their site.", "talk": None},
    "phone_from_website": {"tier": "none", "impact": None,
                           "verify": "The phone came from the business's own website, not a listing. It may be a main line or a cell.",
                           "talk": None},
    "form_untested": {"tier": "none", "impact": None,
                      "verify": "A contact form exists but was not submitted (we never submit forms). Test it yourself before claiming it works or fails.",
                      "talk": None},
}
DEFAULT_ENTRY = {"tier": "weak", "impact": None, "verify": None, "talk": None}
TIER_ORDER = {"strong": 0, "moderate": 1, "weak": 2, "optional": 3, "none": 4}
TIER_LABEL = {"strong": "Strong signal", "moderate": "Moderate signal", "weak": "Minor issue",
              "optional": "Optional feature (low priority)", "none": "Not scored"}


def entry(code: str) -> dict:
    return {**DEFAULT_ENTRY, "actionability": 1.0, **CATALOG.get(code, {})}


def confidence_level(f: dict) -> str:
    return CONFIDENCE_LEVEL.get(f.get("confidence"), "unverified")


def _short(evidence: str, n: int = 110) -> str:
    e = (evidence or "").split(" (Page renders")[0].rstrip(".")
    return e if len(e) <= n else e[: n - 1].rsplit(" ", 1)[0] + "…"


def talking_point(f: dict) -> str | None:
    tpl = entry(f["code"])["talk"]
    if not tpl:
        return None
    values = {"page": f.get("page") or "your site", "evidence_short": _short(f.get("evidence", "")), **(f.get("details") or {})}
    try:
        return tpl.format(**values)
    except (KeyError, IndexError):
        return f"I noticed on your website: {_short(f.get('evidence', ''))}."


def enrich(f: dict) -> dict:
    """Finding plus its interpretation. Works on findings stored by any audit version."""
    e = entry(f["code"])
    out = {**f, "tier": e["tier"], "tier_label": TIER_LABEL[e["tier"]], "confidence_level": confidence_level(f),
           "impact": e["impact"], "verify": e["verify"], "talking_point": talking_point(f)}
    if e["tier"] == "optional":
        out["verify"] = out["verify"] or "Optional feature: not evidence of a problem on its own."
    return out
