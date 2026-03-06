# ukds-mcp — codebase notes

## Purpose
MCP server that provides Claude with tools to search, access, and download datasets from the UK Data Service (UKDS).

## Key files
- `server.py` — entire server implementation (single file, ~570 lines)
- `pyproject.toml` — project config; entry point is `ukds-mcp = "server:mcp.run"`
- `~/.config/ukds-mcp/session.json` — saved session cookies (created after login)

## Architecture
Two API surfaces:
- **AppSync GraphQL** (`ohlhy6cg7nhwtpuer664aeok2i.appsync-api.eu-west-2.amazonaws.com`) — public catalogue search/metadata, API key auth
- **Umbraco Surface REST** (`beta.ukdataservice.ac.uk/Umbraco/Surface/...`) — account/projects/downloads, session cookie auth

Session cookies saved to `~/.config/ukds-mcp/session.json` after Playwright login.

## Tools
- `login` — headless=False Playwright browser; auto-closes after redirect; shows banner
- `search_datasets` / `get_dataset` — GraphQL, no login needed
- `list_projects` — uses `GetProjectsAsLead` + `GetProjectsAsMember` (NOT `GetProjectsForUser` — 404s)
- `get_project` — `GetProject?projectId=...`
- `list_access_requests` — DataTables-style GET with many `columns[n][...]` params
- `list_downloads` — `GetZipName?study=...`
- `add_dataset_to_project` — headless Playwright; injects cookies, navigates to catalogue page, clicks "Add to project"
- `download_file` — tries `/myaccount/data/projects/download` first, falls back to `/Umbraco/Surface/Project/GetDownload`

## Known endpoints (discovered via HAR/network interception)
- `GET /Umbraco/Surface/Project/GetProjectsAsLead` — DataTables params
- `GET /Umbraco/Surface/Project/GetProjectsAsMember` — DataTables params
- `GET /Umbraco/Surface/Project/GetProject?projectId=...`
- `GET /Umbraco/Surface/AccessRequest/GetAccessRequests` — DataTables params
- `GET /Umbraco/Surface/Project/GetZipName?study=...`
- `GET /myaccount/data/projects/download?studyNumber=...&projectId=...&fileName=...` ✓ confirmed working in Chrome history
- `GET /Umbraco/Surface/Project/GetDownload` — fallback, may or may not work

## User's projects (as of 2026-03-06)
- `ea65ef0d-ce0a-4e06-9c22-83fdd67f57a5` — "Ground rents analysis" (active, expires 2026-06-01)
- `ecf0b3c4-29d2-4d8a-931d-0e3773a4ac0b` — "Tax-benefit policy analysis"
- Third project seen in GetProjectsAsLead response (ID not fully captured)

## Outstanding work
- `add_dataset_to_project` uses Playwright UI automation because the direct POST endpoint for adding a dataset to a project was **not yet discovered**. The tool works but is brittle — ideally find the POST endpoint via browser DevTools on the dataset catalogue page (datacatalogue.ukdataservice.ac.uk, not beta.ukdataservice.ac.uk)
- FRS 2016-17 (SN 8336) has **not** been downloaded yet — it needs to be added to a project first. Session cookies expired/were not saved in the last session so a fresh `login` is needed.
- The dataset catalogue has moved to `datacatalogue.ukdataservice.ac.uk` — the `add_dataset_to_project` tool navigates to `beta.ukdataservice.ac.uk/datacatalogue/studies/study?id=...` which may redirect

## Common tasks

### Download a dataset end-to-end
1. `login` — browser opens, sign in, closes automatically
2. `list_projects` — pick a project ID
3. `add_dataset_to_project(study_number, project_id)` — adds it
4. `list_downloads(study_number, project_id)` — get file names
5. `download_file(study_number, file_name, project_id, output_path)` — download

### Restart MCP server after code changes
Restart Claude Code or run `claude mcp restart ukds-mcp`.
