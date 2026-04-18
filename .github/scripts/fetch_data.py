#!/usr/bin/env python3
"""
Fetch dashboard data from App Store Connect, Reddit, and Apple Search Ads.
Writes to dashboard/dashboard-data.json.

Required environment variables:
  ASC_KEY_ID       — App Store Connect API key ID (e.g. HXJJLGXCTV)
  ASC_ISSUER_ID    — App Store Connect issuer ID (UUID)
  ASC_PRIVATE_KEY  — Contents of the .p8 private key file

Optional:
  ASC_VENDOR_NUMBER  — Vendor number for Sales Reports (find in ASC > Payments & Financial Reports)
  REDDIT_CLIENT_ID   — Reddit script app client ID (create at reddit.com/prefs/apps, type: script)
  REDDIT_CLIENT_SECRET — Reddit script app secret
  ASA_CLIENT_ID      — Apple Search Ads OAuth client ID
  ASA_TEAM_ID        — Apple Search Ads team ID
  ASA_KEY_ID         — Apple Search Ads API key ID
  ASA_PRIVATE_KEY    — Apple Search Ads API private key
"""

import gzip
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import requests


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

APP_ID = "6761327188"
REDDIT_USERNAME = "SherbertExpress9353"

REDDIT_THREADS = [
    {
        "subreddit": "r/microsoft",
        "threadId": "1s5tgdn",
        "title": "Microsoft Lens",
        "url": "https://www.reddit.com/r/microsoft/comments/1s5tgdn/",
    },
    {
        "subreddit": "r/microsoft",
        "threadId": "1qbpz5u",
        "title": "Enraged at the retirement of Microsoft Lens",
        "url": "https://www.reddit.com/r/microsoft/comments/1qbpz5u/",
    },
    {
        "subreddit": "r/OneNote",
        "threadId": "1q9tf6r",
        "title": "Office lens retirement — scanning to OneNote",
        "url": "https://www.reddit.com/r/OneNote/comments/1q9tf6r/",
    },
    {
        "subreddit": "r/ios",
        "threadId": "1ryowra",
        "title": "End of an era: Microsoft Lens officially retired",
        "url": "https://www.reddit.com/r/ios/comments/1ryowra/",
    },
    {
        "subreddit": "r/SideProject",
        "threadId": "1skmsxn",
        "title": "I built a free whiteboard scanner (our post)",
        "url": "https://www.reddit.com/r/SideProject/comments/1skmsxn/",
    },
]


# ─────────────────────────────────────────────
# App Store Connect API
# ─────────────────────────────────────────────

def generate_asc_token(key_id: str, issuer_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1",
    }
    token = jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})
    return token


def asc_get(path: str, token: str, params: dict = None) -> dict:
    url = f"https://api.appstoreconnect.apple.com{path}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_app_rating() -> tuple[Optional[float], Optional[int]]:
    """Returns (average_rating, total_review_count) via iTunes public lookup API."""
    try:
        resp = requests.get(
            f"https://itunes.apple.com/lookup?id={APP_ID}",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("resultCount", 0) > 0:
            r = data["results"][0]
            return r.get("averageUserRating"), r.get("userRatingCount")
        return None, None
    except Exception as e:
        print(f"  [WARN] fetch_app_rating failed: {e}")
        return None, None


def fetch_customer_reviews(token: str, limit: int = 3) -> list[dict]:
    """Returns list of latest reviews [{rating, author, title, body}]."""
    try:
        data = asc_get(
            f"/v1/apps/{APP_ID}/customerReviews",
            token,
            params={
                "sort": "-createdDate",
                "limit": limit,
                "fields[customerReviews]": "rating,title,body,reviewerNickname,createdDate",
            },
        )
        reviews = []
        for item in data.get("data", []):
            a = item["attributes"]
            reviews.append({
                "rating": a.get("rating"),
                "author": a.get("reviewerNickname"),
                "title": a.get("title"),
                "body": a.get("body"),
            })
        return reviews
    except Exception as e:
        print(f"  [WARN] fetch_customer_reviews failed: {e}")
        return []


def fetch_sales_reports(token: str, vendor_number: str, days: int) -> Optional[int]:
    """Returns total unit downloads for the last `days` days using Sales Reports."""
    try:
        total = 0
        today = datetime.now(timezone.utc).date()

        # Fetch daily reports for each day in range (skip today — report not ready)
        for i in range(1, days + 1):
            report_date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            try:
                url = "https://api.appstoreconnect.apple.com/v1/salesReports"
                params = {
                    "filter[frequency]": "DAILY",
                    "filter[reportType]": "SALES",
                    "filter[reportSubType]": "SUMMARY",
                    "filter[vendorNumber]": vendor_number,
                    "filter[reportDate]": report_date,
                }
                headers = {"Authorization": f"Bearer {token}"}
                resp = requests.get(url, headers=headers, params=params, timeout=30)

                if resp.status_code == 404:
                    continue  # No data for this date
                resp.raise_for_status()

                # Response is gzip-compressed TSV
                with gzip.open(io.BytesIO(resp.content), "rt", encoding="utf-8") as f:
                    lines = f.read().splitlines()

                if len(lines) < 2:
                    continue

                headers_row = lines[0].split("\t")
                try:
                    units_idx = headers_row.index("Units")
                    type_idx = headers_row.index("Product Type Identifier")
                    sku_idx = headers_row.index("Apple Identifier")
                except ValueError:
                    continue

                for line in lines[1:]:
                    cols = line.split("\t")
                    if len(cols) <= max(units_idx, type_idx, sku_idx):
                        continue
                    # Filter to our app (type 1 = free, 1F = free update)
                    if str(cols[sku_idx]) == APP_ID and cols[type_idx] in ("1", "1F", "F1"):
                        try:
                            total += int(float(cols[units_idx]))
                        except (ValueError, IndexError):
                            pass

            except Exception:
                pass  # Skip individual date failures silently

        return total if total > 0 else None

    except Exception as e:
        print(f"  [WARN] fetch_sales_reports failed: {e}")
        return None


# ─────────────────────────────────────────────
# Reddit API (public, no auth)
# ─────────────────────────────────────────────

def find_comment_score(comments_data: list, username: str) -> Optional[int]:
    """Recursively search comment tree for our username, return score."""
    for item in comments_data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        data = item.get("data", {})

        if kind == "t1":  # Comment
            if data.get("author", "").lower() == username.lower():
                return data.get("score")
            # Check replies
            replies = data.get("replies")
            if isinstance(replies, dict):
                children = replies.get("data", {}).get("children", [])
                result = find_comment_score(children, username)
                if result is not None:
                    return result

        elif kind == "Listing":
            children = data.get("children", [])
            result = find_comment_score(children, username)
            if result is not None:
                return result

    return None


_reddit_token: Optional[str] = None


def get_reddit_oauth_token() -> Optional[str]:
    """Get Reddit OAuth token using client credentials (read-only public data)."""
    global _reddit_token
    if _reddit_token:
        return _reddit_token

    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=requests.auth.HTTPBasicAuth(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": f"whiteboard-lens-dashboard/1.0 by /u/{REDDIT_USERNAME}"},
            timeout=15,
        )
        resp.raise_for_status()
        _reddit_token = resp.json().get("access_token")
        return _reddit_token
    except Exception as e:
        print(f"  [WARN] Reddit OAuth failed: {e}")
        return None


def fetch_reddit_score(subreddit_name: str, thread_id: str) -> Optional[int]:
    """Fetch our comment score from a Reddit thread."""
    sub = subreddit_name.lstrip("r/")
    ua = f"whiteboard-lens-dashboard/1.0 by /u/{REDDIT_USERNAME}"

    oauth_token = get_reddit_oauth_token()
    if oauth_token:
        base_url = f"https://oauth.reddit.com/r/{sub}/comments/{thread_id}"
        headers = {
            "Authorization": f"bearer {oauth_token}",
            "User-Agent": ua,
        }
    else:
        print(f"  [INFO] No Reddit OAuth — trying public API (may fail from datacenter IPs)")
        base_url = f"https://www.reddit.com/r/{sub}/comments/{thread_id}"
        headers = {"User-Agent": ua}

    try:
        resp = requests.get(base_url + ".json", headers=headers, timeout=15)
        if resp.status_code == 429:
            print(f"  [WARN] Reddit rate limited for {thread_id}")
            return None
        resp.raise_for_status()
        listing = resp.json()

        if len(listing) < 2:
            return None

        comments_listing = listing[1]
        children = comments_listing.get("data", {}).get("children", [])
        score = find_comment_score(children, REDDIT_USERNAME)
        return score

    except Exception as e:
        print(f"  [WARN] fetch_reddit_score({thread_id}) failed: {e}")
        return None


# ─────────────────────────────────────────────
# Apple Search Ads API (deferred)
# ─────────────────────────────────────────────

def fetch_asa_data(days: int) -> dict:
    """Fetch Apple Search Ads data. Returns empty structure if not configured."""
    client_id = os.environ.get("ASA_CLIENT_ID", "")
    team_id = os.environ.get("ASA_TEAM_ID", "")
    key_id = os.environ.get("ASA_KEY_ID", "")
    private_key = os.environ.get("ASA_PRIVATE_KEY", "")

    if not all([client_id, team_id, key_id, private_key]):
        print("  [INFO] ASA credentials not configured — skipping")
        return {"totalSpend": None, "keywords": []}

    # ASA OAuth + Campaign Reports API
    # Implementation added when ASA account is created
    print("  [INFO] ASA credentials present but fetch not yet implemented")
    return {"totalSpend": None, "keywords": []}


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    # Load ASC credentials
    key_id = os.environ.get("ASC_KEY_ID", "")
    issuer_id = os.environ.get("ASC_ISSUER_ID", "")
    private_key = os.environ.get("ASC_PRIVATE_KEY", "")
    vendor_number = os.environ.get("ASC_VENDOR_NUMBER", "")

    if not all([key_id, issuer_id, private_key]):
        print("ERROR: ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY must all be set")
        raise SystemExit(1)

    print("Generating App Store Connect token…")
    token = generate_asc_token(key_id, issuer_id, private_key)

    # ── App rating (public iTunes API, no auth needed) ──
    print("Fetching app rating…")
    rating, review_count = fetch_app_rating()
    print(f"  Rating: {rating}, Reviews: {review_count}")

    print("Fetching latest reviews…")
    latest_reviews = fetch_customer_reviews(token)
    print(f"  Got {len(latest_reviews)} reviews")

    # ── Downloads (sales reports) ──
    downloads_7d = None
    downloads_30d = None
    if vendor_number:
        print("Fetching sales reports (7d)…")
        downloads_7d = fetch_sales_reports(token, vendor_number, 7)
        print(f"  Downloads 7d: {downloads_7d}")

        print("Fetching sales reports (30d)…")
        downloads_30d = fetch_sales_reports(token, vendor_number, 30)
        print(f"  Downloads 30d: {downloads_30d}")
    else:
        print("[INFO] ASC_VENDOR_NUMBER not set — skipping sales reports")
        print("       Find your vendor number in ASC > Payments & Financial Reports")

    # ── Reddit scores ──
    reddit_results = []
    for thread in REDDIT_THREADS:
        print(f"Fetching Reddit score for {thread['threadId']}…")
        score = fetch_reddit_score(thread["subreddit"], thread["threadId"])
        print(f"  Score: {score}")
        reddit_results.append({**thread, "score": score})
        time.sleep(2)  # Be polite to Reddit API

    # ── ASA ──
    print("Fetching Apple Search Ads data (7d)…")
    asa_7d = fetch_asa_data(7)
    print("Fetching Apple Search Ads data (30d)…")
    asa_30d = fetch_asa_data(30)

    # ── Build output ──
    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "acquisition": {
            "7d": {
                "downloads": downloads_7d,
                "impressions": None,   # Requires Analytics Reports API (complex flow)
                "pageViews": None,     # Requires Analytics Reports API
                "conversionRate": None,
                "sources": {"asa": None, "organic": None, "web": None},
            },
            "30d": {
                "downloads": downloads_30d,
                "impressions": None,
                "pageViews": None,
                "conversionRate": None,
                "sources": {"asa": None, "organic": None, "web": None},
            },
        },
        "asa": {
            "7d": asa_7d,
            "30d": asa_30d,
        },
        "productHealth": {
            "rating": rating,
            "reviewCount": review_count,
            "prevWeekReviewCount": None,  # Will be populated on second run
            "latestReviews": latest_reviews,
        },
        "reddit": reddit_results,
    }

    # Write output
    out_path = "dashboard/dashboard-data.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {out_path}")
    print(f"  Rating: {rating} ({review_count} reviews)")
    print(f"  Downloads 7d: {downloads_7d}, 30d: {downloads_30d}")
    reddit_scores = [(t["subreddit"], t["score"]) for t in reddit_results]
    print(f"  Reddit: {reddit_scores}")


if __name__ == "__main__":
    main()
