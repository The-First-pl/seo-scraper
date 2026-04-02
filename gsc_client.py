from __future__ import annotations

from datetime import date, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def credentials_from_dict(data: dict) -> Credentials:
    creds = Credentials(
        token=data["token"],
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def get_service(creds_data: dict):
    return build("searchconsole", "v1", credentials=credentials_from_dict(creds_data))


def list_properties(creds_data: dict) -> list[str]:
    service = get_service(creds_data)
    result = service.sites().list().execute()
    return [s["siteUrl"] for s in result.get("siteEntry", [])]


def fetch_page_queries(service, site_url: str, page_url: str, days: int = 90) -> list[dict]:
    end_date = date.today() - timedelta(days=3)
    start_date = end_date - timedelta(days=days - 1)

    body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "dimensions": ["query"],
        "dimensionFilterGroups": [{
            "filters": [{
                "dimension": "page",
                "operator": "equals",
                "expression": page_url,
            }]
        }],
        "rowLimit": 50,
    }

    try:
        resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        rows = resp.get("rows", [])
        results = [
            {
                "query": row["keys"][0],
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "position": round(row.get("position", 0), 1),
            }
            for row in rows
        ]
        results.sort(key=lambda x: x["clicks"], reverse=True)
        return results
    except Exception:
        return []
