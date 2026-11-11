#!/usr/bin/env python3
"""
notion_to_todoist_sync.py

Watches for NEW tasks in a Notion Tasks database where Responsible = <user>,
and creates corresponding tasks in Todoist Inbox with configurable labels.

Designed to run every few minutes via cron. Keeps track of already-synced
Notion page IDs in ~/.notion_to_todoist_synced.json to avoid duplicates.

Uses only the Python standard library — no pip installs required.

Configuration is loaded from environment variables (see .env.example).
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — all values come from environment variables
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(
            f"ERROR: missing required environment variable: {name}\n"
            f"See .env.example for the full list.\n"
        )
        sys.exit(1)
    return value


TODOIST_API_TOKEN = _require_env("TODOIST_API_TOKEN")
NOTION_API_TOKEN = _require_env("NOTION_API_TOKEN")

# Notion data source ID for the Tasks database
NOTION_DATA_SOURCE_ID = _require_env("NOTION_DATA_SOURCE_ID")

# Notion user ID of the person whose tasks should be synced
NOTION_USER_ID = _require_env("NOTION_USER_ID")

# Todoist target project (e.g. Inbox project ID)
TODOIST_INBOX_ID = _require_env("TODOIST_INBOX_ID")

# Comma-separated list of labels to add to created Todoist tasks
TODOIST_LABELS = [
    label.strip()
    for label in os.environ.get("TODOIST_LABELS", "notion_task").split(",")
    if label.strip()
]

# API base URLs
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2025-09-03"
TODOIST_API_BASE = "https://api.todoist.com/api/v1"

# Local state file – remembers which Notion pages have already been synced
SYNCED_IDS_FILE = Path.home() / ".notion_to_todoist_synced.json"

# Log file
LOG_FILE = Path.home() / ".notion_to_todoist_sync.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("notion_to_todoist_sync")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_synced_ids() -> set:
    """Load the set of already-synced Notion page IDs from disk."""
    if SYNCED_IDS_FILE.exists():
        try:
            data = json.loads(SYNCED_IDS_FILE.read_text(encoding="utf-8"))
            return set(data)
        except (json.JSONDecodeError, TypeError):
            return set()
    return set()


def _save_synced_ids(ids: set) -> None:
    """Persist the set of synced IDs to disk."""
    SYNCED_IDS_FILE.write_text(
        json.dumps(sorted(ids), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _http_request(url, method="GET", headers=None, body=None):
    """
    Perform an HTTP request and return parsed JSON.
    Logs errors and re-raises on failure.
    """
    headers = headers or {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "HTTP %s %s -> %s %s\nResponse body: %s",
            method, url, exc.code, exc.reason, resp_body,
        )
        raise
    except urllib.error.URLError as exc:
        logger.error("URL error for %s: %s", url, exc.reason)
        raise


def _notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def _todoist_headers():
    return {
        "Authorization": f"Bearer {TODOIST_API_TOKEN}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Notion: fetch tasks where Responsible = <user> and Status != Done/Archived
# ---------------------------------------------------------------------------


def fetch_notion_tasks() -> list:
    """
    Query the Notion data source for tasks where:
    - Responsible contains the configured user
    - Status is NOT 'Done' or 'Archived'

    Returns a list of pages with their properties.
    """
    url = f"{NOTION_API_BASE}/data_sources/{NOTION_DATA_SOURCE_ID}/query"

    payload = {
        "filter": {
            "and": [
                {
                    "property": "Responsible",
                    "people": {"contains": NOTION_USER_ID},
                },
                {
                    "property": "Status",
                    "status": {"does_not_equal": "Done"},
                },
                {
                    "property": "Status",
                    "status": {"does_not_equal": "Archived"},
                },
            ]
        },
        "page_size": 100,
    }

    all_results = []
    has_more = True
    start_cursor = None

    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor

        response = _http_request(url, method="POST", headers=_notion_headers(), body=payload)
        results = response.get("results", [])
        all_results.extend(results)

        has_more = response.get("has_more", False)
        start_cursor = response.get("next_cursor")

    return all_results


def fetch_page_title(page_id: str) -> str:
    """Fetch the title of a Notion page by its ID."""
    url = f"{NOTION_API_BASE}/pages/{page_id}"
    try:
        response = _http_request(url, method="GET", headers=_notion_headers())
        props = response.get("properties", {})
        for prop_name in ["Name", "name", "Title", "title", "Project name"]:
            prop = props.get(prop_name, {})
            if prop.get("type") == "title":
                title_parts = prop.get("title", [])
                return "".join(part.get("plain_text", "") for part in title_parts)
        for prop_name, prop in props.items():
            if prop.get("type") == "title":
                title_parts = prop.get("title", [])
                return "".join(part.get("plain_text", "") for part in title_parts)
    except Exception as exc:
        logger.debug("Could not fetch page title for %s: %s", page_id, exc)
    return ""


def _rich_text_to_str(rich_text: list) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text)


def _blocks_to_text(blocks: list) -> str:
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        bdata = block.get(btype, {})
        rt = bdata.get("rich_text", [])
        text = _rich_text_to_str(rt)

        if btype == "heading_1":
            lines.append(f"# {text}")
        elif btype == "heading_2":
            lines.append(f"## {text}")
        elif btype == "heading_3":
            lines.append(f"### {text}")
        elif btype == "bulleted_list_item":
            lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            lines.append(f"• {text}")
        elif btype == "to_do":
            prefix = "☑" if bdata.get("checked", False) else "☐"
            lines.append(f"{prefix} {text}")
        elif btype == "quote":
            lines.append(f"> {text}")
        elif btype == "callout":
            emoji = bdata.get("icon", {}).get("emoji", "")
            lines.append(f"{emoji} {text}".strip())
        elif btype == "divider":
            lines.append("---")
        elif btype == "paragraph":
            if text:
                lines.append(text)
        else:
            if text:
                lines.append(text)

    return "\n".join(lines).strip()


def fetch_page_content(page_id: str) -> str:
    """Fetch the text body of a Notion page by reading its top-level blocks."""
    url = f"{NOTION_API_BASE}/blocks/{page_id}/children?page_size=100"
    try:
        response = _http_request(url, method="GET", headers=_notion_headers())
        blocks = response.get("results", [])
        return _blocks_to_text(blocks)
    except Exception as exc:
        logger.debug("Could not fetch page content for %s: %s", page_id, exc)
    return ""


def extract_task_info(page: dict) -> dict:
    """Extract task name, due date, project name, and page ID from a Notion page."""
    page_id = page.get("id", "")

    props = page.get("properties", {})
    task_name_prop = props.get("Task name", {})
    title_parts = task_name_prop.get("title", [])
    task_name = "".join(part.get("plain_text", "") for part in title_parts)

    due_prop = props.get("Due", {})
    due_date = None
    if due_prop and due_prop.get("date"):
        due_date = due_prop["date"].get("start")

    project_name = ""
    project_prop = props.get("Project", {})
    if project_prop.get("type") == "relation":
        relations = project_prop.get("relation", [])
        if relations:
            project_page_id = relations[0].get("id", "")
            if project_page_id:
                project_name = fetch_page_title(project_page_id)

    page_content = fetch_page_content(page_id)

    return {
        "page_id": page_id,
        "task_name": task_name,
        "due_date": due_date,
        "project_name": project_name,
        "page_content": page_content,
    }


# ---------------------------------------------------------------------------
# Todoist: create a task
# ---------------------------------------------------------------------------


def create_todoist_task(
    task_name: str,
    due_date: str = None,
    project_name: str = "",
    page_content: str = "",
    page_id: str = "",
) -> dict:
    """Create a new task in Todoist with the configured labels."""
    url = f"{TODOIST_API_BASE}/tasks"

    payload = {
        "content": task_name,
        "project_id": TODOIST_INBOX_ID,
        "labels": TODOIST_LABELS,
    }

    description_parts = []
    if project_name:
        description_parts.append(f"Projekt: {project_name}")
    if page_id:
        notion_url = f"https://notion.so/{page_id.replace('-', '')}"
        description_parts.append(f"Notion: {notion_url}")
    if page_content:
        description_parts.append("")  # blank line before body
        description_parts.append(page_content)

    if description_parts:
        payload["description"] = "\n".join(description_parts)[:16383]

    if due_date:
        payload["due_date"] = due_date

    response = _http_request(url, method="POST", headers=_todoist_headers(), body=payload)
    return response


# ---------------------------------------------------------------------------
# Todoist: close tasks whose Notion page is already Done / Archived
# ---------------------------------------------------------------------------

# Label that marks Todoist tasks created by this script
SYNC_LABEL = "notion_task"

# Notion statuses that mean "finished" -> close the Todoist task
CLOSED_STATUSES = {"Done", "Archived"}

DRY_RUN = "--dry-run" in sys.argv or os.environ.get("SYNC_DRY_RUN") == "1"

_NOTION_ID_RE = re.compile(r"notion\.so/(?:[^\s/]*-)?([0-9a-f]{32})", re.IGNORECASE)


def _normalize_page_id(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def fetch_open_synced_todoist_tasks() -> list:
    """All open Todoist tasks (any project) carrying the sync label."""
    tasks = []
    cursor = None
    while True:
        url = f"{TODOIST_API_BASE}/tasks?label={urllib.parse.quote(SYNC_LABEL)}&limit=200"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        response = _http_request(url, method="GET", headers=_todoist_headers())
        tasks.extend(response.get("results", []))
        cursor = response.get("next_cursor")
        if not cursor:
            return tasks


def fetch_notion_status(page_id: str):
    """Return the page's Status name, 'Archived' for trashed pages, None on error."""
    response = _http_request(f"{NOTION_API_BASE}/pages/{page_id}", method="GET", headers=_notion_headers())
    if response.get("in_trash") or response.get("archived"):
        return "Archived"
    status = response.get("properties", {}).get("Status", {}).get("status") or {}
    return status.get("name")


def close_todoist_task(task: dict) -> None:
    """
    Drop the sync label first, then close the task. Without the label the
    reverse sync (todoist_notion_sync.py) ignores the completion, so it never
    re-searches Notion by task name and touches a different page with the
    same title.
    """
    task_id = task["id"]
    labels = [l for l in task.get("labels", []) if l != SYNC_LABEL]
    _http_request(f"{TODOIST_API_BASE}/tasks/{task_id}", method="POST",
                  headers=_todoist_headers(), body={"labels": labels})
    _http_request(f"{TODOIST_API_BASE}/tasks/{task_id}/close", method="POST",
                  headers=_todoist_headers())


def close_finished_tasks(active_page_ids: set) -> int:
    """
    Close open Todoist tasks whose Notion page is Done/Archived.
    Only pages that dropped out of the active query are checked, and only an
    explicit Done/Archived status closes a task (a reassigned task stays open).
    """
    closed = 0
    for task in fetch_open_synced_todoist_tasks():
        match = _NOTION_ID_RE.search(task.get("description", "") or "")
        if not match:
            continue
        page_id = match.group(1).lower()
        if page_id in active_page_ids:
            continue
        try:
            status = fetch_notion_status(page_id)
        except Exception as exc:
            logger.warning("  Could not read Notion status for '%s': %s", task.get("content"), exc)
            continue
        if status not in CLOSED_STATUSES:
            logger.debug("  '%s' not active for user but status=%s -> keep open.", task.get("content"), status)
            continue
        if DRY_RUN:
            logger.info("  [dry-run] Would close Todoist %s '%s' (Notion: %s).", task["id"], task.get("content"), status)
            closed += 1
            continue
        try:
            close_todoist_task(task)
            logger.info("  Closed Todoist %s '%s' (Notion: %s).", task["id"], task.get("content"), status)
            closed += 1
        except Exception as exc:
            logger.error("  Failed to close Todoist %s '%s': %s", task["id"], task.get("content"), exc)
    return closed


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------


def main():
    logger.info("=" * 60)
    logger.info("Starting Notion -> Todoist sync run")
    logger.info("=" * 60)

    synced_ids = _load_synced_ids()
    logger.info("Loaded %d previously synced Notion page IDs.", len(synced_ids))

    try:
        notion_pages = fetch_notion_tasks()
    except Exception as exc:
        logger.error("Failed to fetch Notion tasks: %s", exc)
        return

    logger.info("Notion returned %d active task(s).", len(notion_pages))

    new_count = 0
    skipped_count = 0

    for page in notion_pages:
        info = extract_task_info(page)
        page_id = info["page_id"]
        task_name = info["task_name"]
        due_date = info["due_date"]
        project_name = info["project_name"]

        if not task_name:
            logger.warning("  Skipping page %s - no task name found.", page_id)
            continue

        if page_id in synced_ids:
            skipped_count += 1
            logger.debug("  Skipping '%s' (already synced).", task_name)
            continue

        logger.info("  New task: '%s' (project: %s, due: %s)", task_name, project_name or "none", due_date or "none")

        if DRY_RUN:
            logger.info("    [dry-run] Would create in Todoist.")
            continue

        try:
            result = create_todoist_task(
                task_name, due_date, project_name,
                info.get("page_content", ""), page_id,
            )
            todoist_id = result.get("id", "unknown")
            logger.info("    Created in Todoist (ID: %s) with labels %s", todoist_id, TODOIST_LABELS)
            synced_ids.add(page_id)
            new_count += 1
        except Exception as exc:
            logger.error("    Failed to create Todoist task for '%s': %s", task_name, exc)

    if not DRY_RUN:
        _save_synced_ids(synced_ids)

    # Notion query succeeded, so the active set is trustworthy -> close finished ones
    active_page_ids = {_normalize_page_id(p.get("id", "")) for p in notion_pages}
    try:
        closed_count = close_finished_tasks(active_page_ids)
    except Exception as exc:
        logger.error("Failed to close finished Todoist tasks: %s", exc)
        closed_count = 0

    logger.info("-" * 60)
    logger.info(
        "Sync complete%s: %d new task(s) created in Todoist, %d already synced (skipped), %d closed.",
        " [dry-run]" if DRY_RUN else "", new_count, skipped_count, closed_count,
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
