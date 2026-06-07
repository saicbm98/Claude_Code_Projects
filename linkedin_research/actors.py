"""Actor registry + Apify REST client.

Actors are not hardcoded into the business logic. They live here in a small
registry so the tool can pick the right actor per job (resolve / confirm /
posts / web-fallback) and so a dead or renamed actor can be swapped in one
place. Each entry was verified live on the Apify Store before being added.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


APIFY_BASE = "https://api.apify.com/v2"


@dataclass(frozen=True)
class Actor:
    """One Apify actor we know how to drive."""

    job: str          # logical role: resolve | confirm | posts | web
    actor_id: str     # username~name form used by the REST API
    human_name: str   # store name, for messages
    notes: str = ""


# Verified live on the Apify Store (high usage, ~99.9% success, "no cookies").
# The whole harvestapi family is one vendor, which keeps URL/profile shapes
# consistent across the resolve -> confirm -> posts pipeline.
REGISTRY: dict[str, Actor] = {
    "resolve": Actor(
        job="resolve",
        actor_id="harvestapi~linkedin-profile-search-by-name",
        human_name="LinkedIn Profile Search By Name (harvestapi)",
        notes="Cheap: ~$0.004 per search page of up to 10 short profiles.",
    ),
    "confirm": Actor(
        job="confirm",
        actor_id="harvestapi~linkedin-profile-scraper",
        human_name="LinkedIn Profile Scraper (harvestapi)",
        notes="~$0.002-0.004 per profile. Confirms current role/headline.",
    ),
    "posts": Actor(
        job="posts",
        actor_id="harvestapi~linkedin-profile-posts",
        human_name="LinkedIn Profile Posts Scraper (harvestapi)",
        notes="~$0.0015-0.002 per post/comment/reaction item.",
    ),
    "web": Actor(
        job="web",
        actor_id="apify~google-search-scraper",
        human_name="Google Search Results Scraper (apify, official)",
        notes="Optional fallback for non-LinkedIn public activity.",
    ),
}


class ApifyError(RuntimeError):
    pass


@dataclass
class ApifyClient:
    token: str
    timeout: int = 300
    _last_dataset_id: str | None = field(default=None, init=False)

    def run_actor(self, actor_id: str, run_input: dict[str, Any]) -> list[dict]:
        """Run an actor synchronously and return its dataset items.

        Uses run-sync-get-dataset-items so we make exactly one HTTP call per
        actor run and never poll redundantly.
        """
        url = (
            f"{APIFY_BASE}/acts/{actor_id}/run-sync-get-dataset-items"
            f"?token={self.token}"
        )
        data = json.dumps(run_input).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise ApifyError(
                f"Actor {actor_id} failed (HTTP {exc.code}): {body[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ApifyError(f"Network error calling {actor_id}: {exc.reason}") from exc

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ApifyError(f"Bad JSON from {actor_id}: {payload[:300]}") from exc
