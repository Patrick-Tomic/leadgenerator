"""Optional Claude-written lead brief and outreach draft.

Runs only when the user clicks "Write AI brief" for a lead, so there is no cost
unless you ask. Credentials come from ANTHROPIC_API_KEY (or an `ant auth login`
profile). Claude only sees the evidence we collected and is told not to invent more.
"""
import json
import os
from pathlib import Path

import anthropic

from . import config

SYSTEM = """You write sales prep notes for {company}. {description}
Services they offer: {offerings}.

You get one researched lead with the evidence our automated audit collected. Write:
1. **Why this lead**: 2-3 sentences on the strongest, evidence-backed opportunity.
2. **What to offer**: a short bulleted list of specific services, each tied to a finding.
3. **Talking points**: up to 3 bullets for a phone call, in plain language, each stating something we observed and why it may matter. No jargon.
4. **Outreach email**: a short, friendly, non-pushy first email (under 130 words) with a subject line. Mention one or two concrete observations. No fake familiarity, no invented statistics, no claims we didn't observe.
5. **Verify before contacting**: anything that's low-confidence or should be checked by hand.

Only use facts in the lead data. Lead with the strongest evidence (see "score_explanation"); treat "optional" tier findings (no chat widget, no booking tool, ...) as low priority and never as proof of a problem. Never claim the business is losing customers, leads or revenue; say what we observed and why it *may* matter. Never claim a form fails: forms are never submitted. Each finding has a "kind": "confirmed" (observed), "potential" (missing optional feature or low confidence) or "unavailable" (we couldn't check). Never present potential or unavailable items as confirmed problems. Use Markdown."""


def available() -> bool:
    return bool(
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("ANTHROPIC_AUTH_TOKEN")
        or Path("~/.config/anthropic").expanduser().exists()
    )


def _lead_payload(lead: dict) -> dict:
    return {
        "name": lead["name"],
        "industry": config.INDUSTRIES.get(lead["industry"], {}).get("label", lead["industry"]),
        "location": lead.get("address") or lead.get("city"),
        "phone": lead.get("phone"),
        "email": lead.get("email"),
        "website": lead.get("final_url") or lead.get("website"),
        "website_status": lead.get("website_status"),
        "score": lead.get("score"),
        "opportunities": lead.get("opportunities"),
        "score_explanation": lead.get("explanation"),
        "all_findings": [{k: f.get(k) for k in ("title", "evidence", "page", "confidence_level", "tier", "kind", "impact", "verify")}
                         for f in lead.get("findings") or []],
    }


async def write_brief(lead: dict, profile: dict) -> str:
    if not available():
        raise RuntimeError("AI briefs need Claude API credentials. Set ANTHROPIC_API_KEY and restart.")
    client = anthropic.AsyncAnthropic()
    try:
        response = await client.beta.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=4000,
            system=SYSTEM.format(company=profile["name"], description=profile.get("description", ""),
                                 offerings=", ".join(o["label"] for o in profile["offerings"].values())),
            output_config={"effort": "low"},
            # If a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": "Lead data:\n```json\n" + json.dumps(_lead_payload(lead), indent=2) + "\n```"}],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("Claude API credentials are missing or invalid. Set ANTHROPIC_API_KEY and restart.")
    except anthropic.RateLimitError:
        raise RuntimeError("Claude API rate limit hit; try again in a minute.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach the Claude API.")
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this brief.")
    return "".join(b.text for b in response.content if b.type == "text").strip()
