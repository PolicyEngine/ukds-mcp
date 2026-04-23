"""
UK Data Service MCP server.

Two API surfaces:
  - AppSync GraphQL (public, API-key auth) — catalogue search and metadata
  - Umbraco Surface REST (session-cookie auth) — account, projects, downloads

Tools:
  login               — opens browser for Shibboleth sign-in; captures session cookies
  search_datasets     — search the catalogue (GraphQL, no login needed)
  get_dataset         — full metadata for a study (GraphQL, no login needed)
  list_projects       — list your UKDS projects
  get_project         — details of a specific project
  list_access_requests — datasets you have active access to
  list_downloads      — files available for a study in a project
  download_file       — download a data file to a local path
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BETA_BASE = "https://beta.ukdataservice.ac.uk"
GRAPHQL_URL = "https://ohlhy6cg7nhwtpuer664aeok2i.appsync-api.eu-west-2.amazonaws.com/graphql"
SESSION_FILE = Path.home() / ".config" / "ukds-mcp" / "session.json"

mcp = FastMCP("ukds")


# ---------------------------------------------------------------------------
# Session management (Umbraco cookie auth)
# ---------------------------------------------------------------------------

def _load_session() -> dict[str, str]:
    if SESSION_FILE.exists():
        SESSION_FILE.chmod(0o600)
        return json.loads(SESSION_FILE.read_text())
    return {}


def _save_session(cookies: dict[str, str]) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    SESSION_FILE.parent.chmod(0o700)
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cookies, f, indent=2)
        f.write("\n")
    SESSION_FILE.chmod(0o600)


def _make_client(cookies: dict[str, str] | None = None) -> httpx.Client:
    jar = httpx.Cookies()
    for k, v in (cookies or _load_session()).items():
        jar.set(k, v, domain="beta.ukdataservice.ac.uk")
    return httpx.Client(
        base_url=BETA_BASE,
        cookies=jar,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": BETA_BASE + "/",
        },
        follow_redirects=True,
        timeout=30,
    )


def _check_auth(client: httpx.Client) -> bool:
    r = client.get("/Umbraco/Surface/Login/KeepAlive")
    return r.status_code == 200


# ---------------------------------------------------------------------------
# GraphQL helper (catalogue — no login required)
# ---------------------------------------------------------------------------

def _graphql_api_key() -> str:
    key = (
        os.environ.get("UKDS_GRAPHQL_API_KEY")
        or os.environ.get("GRAPHQL_API_KEY")
        or ""
    ).strip()
    if not key:
        raise RuntimeError(
            "UKDS_GRAPHQL_API_KEY must be set to use the UKDS catalogue GraphQL API."
        )
    return key


def _gql(query: str, variables: dict | None = None) -> dict:
    r = httpx.post(
        GRAPHQL_URL,
        headers={
            "x-api-key": _graphql_api_key(),
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def login() -> str:
    """
    Open a browser window for you to sign in to the UK Data Service.
    Completes the Shibboleth/institutional login flow and saves the session
    automatically — you won't need to log in again until the session expires.
    """
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    import asyncio

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Show a clear instruction overlay before navigating
        async def show_banner(p):
            try:
                await p.evaluate("""() => {
                    const div = document.createElement('div');
                    div.id = 'ukds-mcp-banner';
                    div.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;background:#1a73e8;color:#fff;font:bold 15px/40px sans-serif;text-align:center;padding:0 16px;';
                    div.textContent = 'Sign in to the UK Data Service — the browser will close automatically once you are logged in.';
                    document.body.prepend(div);
                }""")
            except Exception:
                pass

        page.on("load", lambda: asyncio.ensure_future(show_banner(page)))

        await page.goto(f"{BETA_BASE}/myaccount/login")

        print("Browser opened — please sign in to the UK Data Service.")
        print("The browser will close automatically once login is complete.")

        try:
            await page.wait_for_url(
                lambda url: "ukdataservice.ac.uk/myaccount" in url.lower() and "login" not in url.lower(),
                timeout=300_000,
            )
        except PlaywrightTimeout:
            await browser.close()
            return "Login timed out after 5 minutes. Please try again."

        # Give the page a moment to fully settle and set all cookies
        await asyncio.sleep(2)

        try:
            all_cookies = await ctx.cookies()
        except Exception:
            all_cookies = []

        session_cookies = {
            c["name"]: c["value"]
            for c in all_cookies
            if "ukdataservice.ac.uk" in c.get("domain", "")
        }

        try:
            await browser.close()
        except Exception:
            pass
        print("Browser closed — login complete.")

        if not session_cookies:
            return "Login appeared to succeed but no session cookies were captured. Please try again."

        _save_session(session_cookies)

        with _make_client(session_cookies) as client:
            if _check_auth(client):
                return (
                    f"Logged in successfully. Session saved ({len(session_cookies)} cookies). "
                    "You're ready to use all UKDS tools."
                )
            else:
                return "Cookies captured but auth check failed — the session may not be valid. Try logging in again."


@mcp.tool()
def search_datasets(
    query: str,
    rows: int = 10,
    date_from: int | None = None,
    date_to: int | None = None,
) -> str:
    """
    Search the UK Data Service catalogue. Does not require login.

    Args:
        query:     Search terms (e.g. "English Housing Survey", "labour market")
        rows:      Number of results to return (default 10, max 50)
        date_from: Filter to studies from this year (e.g. 2015)
        date_to:   Filter to studies up to this year (e.g. 2023)
    """
    rows = min(rows, 50)

    gql = """
    query SearchStudies($q: String, $rows: Int, $from: Int, $to: Int) {
      getStudyList(QueryString: $q, Rows: $rows, DateFrom: $from, DateTo: $to, Start: 0, Sort: 0) {
        Count
        Results {
          FriendlyId
          Title
          KindOfData
          LatestEditionReleaseDate
          Embargoed
        }
      }
    }
    """
    data = _gql(gql, {
        "q": query,
        "rows": rows,
        "from": date_from,
        "to": date_to,
    })

    if "errors" in data:
        return f"Search error: {data['errors']}"

    result = data["data"]["getStudyList"]
    studies = result.get("Results") or []
    total = result.get("Count", 0)

    if not studies:
        return f"No results found for '{query}'."

    lines = [f"Found {total} studies for '{query}' (showing {len(studies)}):\n"]
    for s in studies:
        fid = s.get("FriendlyId", "")
        title = s.get("Title", "Untitled")
        embargoed = " [EMBARGOED]" if s.get("Embargoed") else ""
        release = s.get("LatestEditionReleaseDate", "")[:10] if s.get("LatestEditionReleaseDate") else ""
        release_str = f" (latest: {release})" if release else ""
        lines.append(f"• SN {fid}: {title}{embargoed}{release_str}")

    return "\n".join(lines)


@mcp.tool()
def get_dataset(study_number: str) -> str:
    """
    Get full metadata for a dataset by its study number (FriendlyId). Does not require login.

    Args:
        study_number: The study number as shown in search results (e.g. '9444')
    """
    gql = """
    query GetStudy($id: String) {
      getStudyItem(FriendlyId: $id) {
        Title
        SubTitle
        Abstract
        DOI
        TypeOfAccess
        Status
        FriendlyId
        TimePeriod
        TimePeriodStart
        TimePeriodEnd
        KindOfData
        Country
        Subject
        Creator { Organisations Individuals }
        Publisher
        Keyword { Value }
        DataFormat { Value }
        SamplingProcedure
        Universe
      }
    }
    """
    data = _gql(gql, {"id": str(study_number)})

    if "errors" in data:
        return f"Error fetching study {study_number}: {data['errors']}"

    item = data["data"]["getStudyItem"]
    if not item:
        return f"Study {study_number} not found."

    return json.dumps(item, indent=2)[:5000]


def _fetch_all_projects(client: httpx.Client) -> list[dict]:
    """Fetch projects where user is lead or member, returning a combined list."""
    projects = []
    seen = set()
    dt_params = {"draw": 1, "start": 0, "length": 999, "search[value]": "", "search[regex]": "false"}
    for endpoint in ("/Umbraco/Surface/Project/GetProjectsAsLead", "/Umbraco/Surface/Project/GetProjectsAsMember"):
        r = client.get(endpoint, params=dt_params)
        if r.status_code == 200:
            for p in (r.json().get("data") or []):
                pid = p.get("projectid") or p.get("projectId") or p.get("id", "")
                if pid and pid not in seen:
                    seen.add(pid)
                    projects.append(p)
    return projects


@mcp.tool()
def list_projects() -> str:
    """
    List all UKDS projects associated with your account. Requires login.
    """
    with _make_client() as client:
        if not _check_auth(client):
            return "Not authenticated. Please run the login tool first."

        projects = _fetch_all_projects(client)

        if not projects:
            return "No projects found on your account."

        lines = ["Your UKDS projects:\n"]
        for p in projects:
            pid = p.get("projectid") or p.get("projectId") or p.get("id", "")
            fid = p.get("friendlyid") or p.get("friendlyId") or p.get("projectFriendlyId", "")
            title = p.get("title") or p.get("projectTitle") or p.get("name", "Untitled")
            state = p.get("statefriendlyname") or p.get("stateName") or p.get("state", "")
            state_str = f" [{state}]" if state else ""
            expiry = (p.get("projectexpirydate") or "")[:10]
            expiry_str = f" (expires {expiry})" if expiry else ""
            lines.append(f"• [{fid}] {title}{state_str}{expiry_str}\n  ID: {pid}")

        return "\n".join(lines)


@mcp.tool()
async def add_dataset_to_project(study_number: int, project_id: str) -> str:
    """
    Add a dataset (study) to one of your UKDS projects so it can be downloaded.
    Requires login. Use list_projects to find your project IDs.

    Args:
        study_number: The numeric study ID (SN), e.g. 8336
        project_id:   The project UUID to add the dataset to
    """
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    import asyncio

    cookies = _load_session()
    if not cookies:
        return "Not authenticated. Please run the login tool first."

    captured_requests: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()

        # Inject saved session cookies
        await ctx.add_cookies([
            {"name": k, "value": v, "domain": "beta.ukdataservice.ac.uk", "path": "/"}
            for k, v in cookies.items()
        ])

        page = await ctx.new_page()

        # Intercept the add-to-project POST so we can discover and replay it
        async def on_request(req):
            if req.method == "POST" and "ukdataservice.ac.uk" in req.url and "matomo" not in req.url:
                captured_requests.append({"url": req.url, "body": req.post_data or ""})

        page.on("request", on_request)

        # Navigate to the dataset page
        await page.goto(f"{BETA_BASE}/datacatalogue/studies/study?id={study_number}", timeout=30_000)
        await asyncio.sleep(2)

        # Look for and click "Add to project" or similar button
        added = False
        for selector in [
            "button:has-text('Add to project')",
            "a:has-text('Add to project')",
            "button:has-text('Add to Project')",
            "[data-action='add-to-project']",
        ]:
            btn = page.locator(selector).first
            if await btn.count() > 0:
                await btn.click()
                await asyncio.sleep(1)
                added = True
                break

        if not added:
            await browser.close()
            return (
                f"Could not find an 'Add to project' button on the study {study_number} page. "
                "The dataset may already be in a project, or the page layout has changed."
            )

        # If a project selector modal appears, choose the right project
        for selector in [
            f"option[value='{project_id}']",
            f"[data-project-id='{project_id}']",
            f"li:has-text('{project_id}')",
        ]:
            opt = page.locator(selector).first
            if await opt.count() > 0:
                await opt.click()
                await asyncio.sleep(0.5)
                break

        # Submit the form if there's a confirm button
        for selector in ["button:has-text('Add')", "button:has-text('Confirm')", "button[type='submit']"]:
            btn = page.locator(selector).first
            if await btn.count() > 0:
                await btn.click()
                await asyncio.sleep(2)
                break

        await asyncio.sleep(2)
        await browser.close()

    if captured_requests:
        print(f"Captured add-to-project requests: {captured_requests}")

    # Verify the dataset now appears in access requests for this project
    with _make_client() as client:
        params: dict[str, Any] = {
            "columns[0][data]": "studyId", "columns[0][orderable]": "true",
            "columns[0][searchable]": "true", "columns[0][search][regex]": "false",
            "columns[0][search][value]": "",
            "columns[1][data]": "title", "columns[1][orderable]": "true",
            "columns[1][searchable]": "true", "columns[1][search][regex]": "false",
            "columns[1][search][value]": "",
            "columns[2][data]": "projectId", "columns[2][orderable]": "true",
            "columns[2][searchable]": "true", "columns[2][search][regex]": "false",
            "columns[2][search][value]": project_id,
            "draw": 1, "length": 999, "order[0][column]": 2, "order[0][dir]": "asc",
            "search[regex]": "false", "search[value]": project_id, "start": 0,
        }
        r = client.get("/Umbraco/Surface/AccessRequest/GetAccessRequests", params=params)
        if r.status_code == 200:
            records = r.json().get("data", [])
            for rec in records:
                if str(rec.get("studyId", "")) == str(study_number) or str(rec.get("friendlyStudyId", "")) == str(study_number):
                    state = rec.get("stateName", "")
                    return f"Study {study_number} is now in project {rec.get('projectTitle', project_id)} with status: {state}."

    return (
        f"Add-to-project action was attempted for study {study_number}. "
        "Please check your UKDS account to confirm it was added successfully, "
        "then use list_downloads to see available files."
    )


@mcp.tool()
def get_project(project_id: str) -> str:
    """
    Get details of a specific UKDS project. Requires login.

    Args:
        project_id: The project UUID (e.g. 'ea65ef0d-ce0a-4e06-9c22-83fdd67f57a5')
    """
    with _make_client() as client:
        r = client.get("/Umbraco/Surface/Project/GetProject", params={"projectId": project_id})
        if r.status_code in (401, 403):
            return "Not authenticated. Please run the login tool first."
        if r.status_code != 200:
            return f"Failed to fetch project (HTTP {r.status_code})."
        return json.dumps(r.json(), indent=2)[:4000]


@mcp.tool()
def list_access_requests(project_id: str | None = None) -> str:
    """
    List datasets you have active access to, optionally filtered by project. Requires login.

    Args:
        project_id: Optional project UUID to filter by
    """
    params: dict[str, Any] = {
        "columns[0][data]": "studyId",
        "columns[0][orderable]": "true",
        "columns[0][searchable]": "true",
        "columns[0][search][regex]": "false",
        "columns[0][search][value]": "",
        "columns[1][data]": "title",
        "columns[1][orderable]": "true",
        "columns[1][searchable]": "true",
        "columns[1][search][regex]": "false",
        "columns[1][search][value]": "",
        "columns[2][data]": "projectId",
        "columns[2][orderable]": "true",
        "columns[2][searchable]": "true",
        "columns[2][search][regex]": "false",
        "columns[2][search][value]": "",
        "draw": 1,
        "length": 999,
        "order[0][column]": 2,
        "order[0][dir]": "asc",
        "search[regex]": "false",
        "search[value]": project_id or "",
        "start": 0,
    }

    with _make_client() as client:
        r = client.get("/Umbraco/Surface/AccessRequest/GetAccessRequests", params=params)
        if r.status_code in (401, 403):
            return "Not authenticated. Please run the login tool first."
        if r.status_code != 200:
            return f"Failed to list access requests (HTTP {r.status_code})."

        d = r.json()
        records = d.get("data", [])

        if not records:
            return "No active access requests found."

        lines = [f"Active dataset access ({len(records)} total):\n"]
        current_project = None
        for rec in records:
            proj = rec.get("projectTitle", "Unknown project")
            pid = rec.get("projectId", "")
            if proj != current_project:
                current_project = proj
                lines.append(f"\nProject: {proj} ({pid})")
            state = rec.get("stateName", "")
            study_id = rec.get("studyId", "")
            title = rec.get("title", "Unknown")
            lines.append(f"  • Study {study_id}: {title} [{state}]")

        return "\n".join(lines)


@mcp.tool()
def list_downloads(study_number: int, project_id: str) -> str:
    """
    List files available to download for a dataset within a project. Requires login.

    Args:
        study_number: The numeric study ID (SN)
        project_id:   The project UUID the dataset is assigned to
    """
    with _make_client() as client:
        r = client.get(
            "/Umbraco/Surface/Project/GetZipName",
            params={"study": study_number},
        )
        if r.status_code in (401, 403):
            return "Not authenticated. Please run the login tool first."
        if r.status_code != 200:
            return f"Failed to list downloads (HTTP {r.status_code})."

        d = r.json()
        files = d.get("data", d) if isinstance(d, dict) else d

        if not files:
            return f"No downloadable files found for study {study_number}."

        lines = [f"Available files for study {study_number}:\n"]
        for f in files:
            name = f.get("fileName") or f.get("name") or f.get("label", "Unknown")
            size = f.get("fileSize") or f.get("size", "")
            size_str = f" ({size:,} bytes)" if isinstance(size, int) and size else f" ({size})" if size else ""
            lines.append(f"  • {name}{size_str}")

        return "\n".join(lines)


@mcp.tool()
def download_file(
    study_number: int,
    file_name: str,
    project_id: str,
    output_path: str,
    file_size: int = 0,
) -> str:
    """
    Download a data file from a UKDS project to a local path. Requires login.

    Args:
        study_number: The numeric study ID (SN)
        file_name:    Exact filename as returned by list_downloads
        project_id:   The project UUID
        output_path:  Local path to save the file (e.g. '/Users/me/data/ehs2023.zip')
        file_size:    File size in bytes (optional, from list_downloads)
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cookies = _load_session()
    if not cookies:
        return "Not authenticated. Please run the login tool first."

    jar = httpx.Cookies()
    for k, v in cookies.items():
        jar.set(k, v, domain="beta.ukdataservice.ac.uk")

    base_headers = {"Referer": BETA_BASE + "/", "X-Requested-With": "XMLHttpRequest"}

    # Try both known download endpoints
    endpoints = [
        ("/myaccount/data/projects/download", {
            "studyNumber": study_number,
            "projectId": project_id,
            "fileName": file_name,
        }),
        ("/Umbraco/Surface/Project/GetDownload", {
            "studyNumber": study_number,
            "projectId": project_id,
            "fileName": file_name,
            **({"fileSize": file_size} if file_size else {}),
        }),
    ]

    with httpx.Client(
        base_url=BETA_BASE,
        cookies=jar,
        headers=base_headers,
        follow_redirects=True,
        timeout=600,
    ) as client:
        for endpoint, params in endpoints:
            with client.stream("GET", endpoint, params=params) as r:
                if r.status_code in (401, 403):
                    return "Not authenticated. Please run the login tool first."
                if r.status_code == 404:
                    continue
                if r.status_code != 200:
                    return f"Download failed (HTTP {r.status_code}) from {endpoint}."

                content_type = r.headers.get("content-type", "")
                if "text/html" in content_type or "application/json" in content_type:
                    body = r.read().decode()[:500]
                    if endpoint == endpoints[0][0]:
                        continue  # try the next endpoint
                    return f"Server returned unexpected content ({content_type}): {body}"

                total = 0
                with open(out, "wb") as fh:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        fh.write(chunk)
                        total += len(chunk)

                return f"Downloaded {file_name} → {out} ({total:,} bytes)"

    return "Download failed: no working endpoint found. Please check your login and try again."


if __name__ == "__main__":
    mcp.run()
