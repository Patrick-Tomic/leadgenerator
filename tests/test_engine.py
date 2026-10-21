from app.analysis.website import inspect_html, is_social
from app.websites import _classify_url
from app.discovery.osm import build_query, element_to_business
from app.scoring import score_lead

OLD_SITE = """<html><head><title>Home</title></head><body>
<center><font size=2>Welcome to Joe's Pool Care</font></center>
<p>Call us at (813) 555-1234 for weekly pool service in Brandon and Riverview. We clean pools,
balance chemicals and repair pumps. Family owned since 1998. Serving Hillsborough County.</p>
<form action="mailto:joe@example.net"><input name=name><input name=phone><textarea name=msg></textarea></form>
<script src="/js/jquery-1.8.3.min.js"></script>
<p>&copy; 2015 Joe's Pool Care</p></body></html>"""

MODERN_SITE = """<html><head><title>Bay Area Roofing | Tampa Roof Repair & Replacement</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Licensed Tampa roofers.">
<script type="application/ld+json">{"@type":"RoofingContractor"}</script>
<script src="https://embed.tawk.to/abc"></script></head><body>
<h1>Tampa's trusted roofers</h1><a href="tel:8135550000">(813) 555-0000</a>
<a href="https://book.housecallpro.com/x">Book online</a>
<form><input name=name><input name=phone type=tel><textarea name=message></textarea></form>
<p>""" + "Quality roofing work across Tampa Bay. " * 30 + """</p><p>© 2026 Bay Area Roofing</p></body></html>"""


def codes(findings):
    return {f["code"] for f in findings}


def test_old_site_findings_have_evidence():
    findings, facts = inspect_html(OLD_SITE, "https://joespool.example")
    c = codes(findings)
    assert {"no_viewport", "mailto_form", "old_jquery", "stale_copyright", "legacy_markup", "weak_title", "no_click_to_call"} <= c
    assert all(f["evidence"] for f in findings)
    assert facts["copyright_year"] == 2015


def test_modern_site_has_few_findings():
    findings, facts = inspect_html(MODERN_SITE, "https://bayarearoofing.example")
    c = codes(findings)
    assert not c & {"no_viewport", "mailto_form", "stale_copyright", "no_click_to_call", "no_title"}
    assert facts["has_form"] and facts["booking"] == ["Housecall Pro"] and facts["chat"] == ["tawk.to"]


def test_parked_page_detected():
    findings, _ = inspect_html("<html><body>This domain is for sale! Buy this domain today.</body></html>", "http://x.example")
    assert codes(findings) == {"parked_or_placeholder"}


def test_scoring_prefers_real_problems():
    biz = {"name": "X", "industry": "pool", "phone": "+1-813-555-1234"}
    weak, _ = inspect_html(MODERN_SITE, "https://a.example")
    strong, _ = inspect_html(OLD_SITE, "https://b.example")
    s_weak = score_lead(biz, weak)
    s_strong = score_lead(biz, strong)
    assert s_strong["score"] > s_weak["score"] + 20
    assert s_weak["grade"] == "D"
    assert s_strong["opportunities"][0]["evidence"]


def test_unverified_no_website_scores_below_confirmed():
    biz = {"name": "X", "industry": "pool", "phone": "1"}
    unv = score_lead(biz, [{"code": "no_website_unverified", "severity": "high", "confidence": "low", "evidence": "e", "title": "t"}])
    conf = score_lead(biz, [{"code": "no_website_confirmed", "severity": "high", "confidence": "high", "evidence": "e", "title": "t"}])
    assert conf["score"] > unv["score"]


def test_osm_element_parsing_and_chain_filter():
    el = {"type": "node", "id": 1, "lat": 27.9, "lon": -82.4, "tags": {
        "name": "Ace Plumbing", "craft": "plumber", "phone": "+1-813-555-0101;+1-813-555-0102",
        "addr:housenumber": "12", "addr:street": "Main St", "addr:city": "Tampa", "website": "aceplumbing.com"}}
    b = element_to_business(el, "plumbing")
    assert b["phone"] == "+1-813-555-0101" and b["address"].startswith("12 Main St, Tampa") and b["website"] == "aceplumbing.com"
    el["tags"]["brand"] = "Roto-Rooter"
    assert element_to_business(el, "plumbing") is None


def test_build_query():
    q = build_query(['["craft"="hvac"]'], (1, 2, 3, 4))
    assert 'nwr["craft"="hvac"](1,2,3,4);' in q and q.startswith("[out:json]")


def test_directory_and_social_urls_are_not_business_sites():
    # Replaces the old name-only matcher (pick_official), which could turn a search miss
    # into a "confirmed no website". Identity is now decided from the candidate page.
    assert _classify_url("https://www.bbb.org/us/fl/tampa/profile/ace") == "directory"
    assert _classify_url("https://www.yelp.com/biz/ace-plumbing-tampa") in ("directory", "social")
    assert _classify_url("https://www.aceplumbingtampa.com/about") is None
    assert is_social("https://m.facebook.com/aceplumbing")


def test_js_shell_findings_downgraded():
    shell = '<html><head></head><body><div id="root"></div>' + '<script src="/a.js"></script>' * 3 + "</body></html>"
    findings, facts = inspect_html(shell, "https://spa.example")
    assert facts["js_rendered"]
    assert all(f["confidence"] == "low" for f in findings if f["code"] in ("no_title", "no_h1", "no_meta_description"))
    assert "thin_content" not in codes(findings)


def test_undecodable_body_not_analyzed():
    findings, facts = inspect_html("�" * 500, "https://x.example")
    assert facts.get("unreadable") and codes(findings) == {"unreadable_page"}
