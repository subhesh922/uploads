from __future__ import annotations

import re
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

# Optional PDF libs — we'll try pdfminer first, then PyPDF2
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text  # type: ignore
except Exception:  # pragma: no cover
    pdfminer_extract_text = None

try:
    import PyPDF2  # type: ignore
except Exception:  # pragma: no cover
    PyPDF2 = None

from server.utils.logger import get_logger
import certifi  # ✅ Added for robust SSL handling

logger = get_logger(__name__)

# =============================================================================
# Public dispatcher (API or PDF)
# =============================================================================

def load_sprll(
    *,
    source_type: str,
    subsystem: Union[str, List[str]],
    project: Optional[str] = None,
    run_id: Optional[str] = None,
    jira_url: Optional[str] = None,
    pat_token: Optional[str] = None,
    auth_mode: str = "bearer",
    username: Optional[str] = None,
    jql: Optional[str] = None,
    fields: Optional[List[str]] = None,
    max_results: int = 1000,
    pdf_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    subsystems = _as_list_subsystem(subsystem)
    proj = (project or "SPRLL").strip()

    if source_type == "api":
        return fetch_sprll_from_api(
            jira_url=jira_url or "",
            pat_token=pat_token,
            subsystem=subsystems,
            project=proj,
            run_id=run_id,
            max_results=max_results,
            auth_mode=auth_mode,
            username=username,
            jql=jql,
            fields=fields,
        )

    if source_type == "pdf":
        if not pdf_path:
            logger.error("[SPRLL] PDF path not provided")
            return []
        return extract_sprll_from_pdf(
            file_path=pdf_path,
            subsystem=subsystems,
            project=proj,
            run_id=run_id,
        )

    logger.error("[SPRLL] Unknown source_type=%s (use 'api' or 'pdf')", source_type)
    return []

# =============================================================================
# API path → normalize to KB-like rows
# =============================================================================

def fetch_sprll_from_api(
    jira_url: str,
    pat_token: Optional[str],
    subsystem: Union[str, List[str]],
    project: Optional[str] = None,
    run_id: Optional[str] = None,
    max_results: int = 1000,
    auth_mode: str = "bearer",
    username: Optional[str] = None,
    jql: Optional[str] = None,
    fields: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:

    subs_list = _as_list_subsystem(subsystem)
    proj = (project or "SPRLL").strip()
    file_name = _compose_pull_filename(proj, run_id)

    if not fields:
        fields = [
            "summary", "description", "priority", "status", "created", "updated",
            "components", "labels",
        ]

    if jira_url is None or jira_url.strip() == "" or requests is None:
        logger.warning("[SPRLL] HTTP client unavailable or URL missing; returning empty list")
        return []

    if not jql:
        def _esc(x: str) -> str:
            return str(x).replace('"', r'\"').strip()

        if subs_list:
            comps = ", ".join([f'"{_esc(s)}"' for s in subs_list])
            jql = f'project = "{_esc(proj)}" AND component IN ({comps})'
        else:
            logger.warning("[SPRLL] No subsystem provided; skipping SPRLL fetch to avoid project-wide pull")
            return []

    logger.info("[SPRLL] ▶️ Fetch | project=%s | subs=%s | max_results=%d", proj, subs_list, max_results)
    logger.debug("[SPRLL] JQL: %s", jql)

    search_url = _join_url(jira_url, "/rest/api/2/search")
    params = {"jql": jql, "maxResults": max_results, "fields": ",".join(fields)}
    headers, auth = _build_auth_headers(auth_mode, pat_token, username)

    try:
        # ✅ safe default using certifi bundle
        resp = requests.get(
            search_url,
            params=params,
            headers=headers,
            auth=auth,
            timeout=60,
            verify=certifi.where(),
        )
        resp.raise_for_status()
        data = resp.json()

    except requests.exceptions.SSLError as ssl_err:
        logger.warning("[SPRLL] ⚠️ SSL verification failed: %s", ssl_err)
        logger.info("[SPRLL] Retrying with corporate root CA if available...")

        try:
            resp = requests.get(
                search_url,
                params=params,
                headers=headers,
                auth=auth,
                timeout=60,
                verify="C:/certs/zebra_root_ca.pem",  # fallback
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("[SPRLL] ❌ SSL retry failed: %s", e)
            return []

    except Exception as e:
        logger.error("[SPRLL] ❌ API error: %s", e)
        return []

    raw_issues = data.get("issues") or []
    logger.info("[SPRLL] Fetched %d issues", len(raw_issues))

    kb_rows, skipped = transform_sprll_issues_to_kb(
        issues=raw_issues,
        subsystems=subs_list,
        project=proj,
        filename=file_name,
        run_id=run_id,
    )
    if skipped:
        logger.warning("[SPRLL] Skipped %d issues with empty summary/description", skipped)

    logger.info("[SPRLL] ✅ Done | emitted=%d | filename=%s", len(kb_rows), file_name)
    return kb_rows

# =============================================================================
# PDF path → normalize to SAME KB-like rows
# =============================================================================

def extract_sprll_from_pdf(
    file_path: str,
    subsystem: Union[str, List[str]],
    project: Optional[str] = None,
    run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    subsystems = _as_list_subsystem(subsystem)
    proj = (project or "SPRLL").strip()
    exact_name = Path(file_path).name

    logger.info("[SPRLL-PDF] ▶️ Parse | file=%s | subs=%s | project=%s", exact_name, subsystems, proj)

    text = _read_pdf_text(file_path)
    if not text:
        logger.error("[SPRLL-PDF] Empty text extracted from %s", exact_name)
        return []

    issues = _parse_pdf_into_issues(text)

    kb_rows, skipped = transform_sprll_issues_to_kb(
        issues=issues,
        subsystems=subsystems,
        project=proj,
        filename=exact_name,
        run_id=run_id,
    )
    if skipped:
        logger.warning("[SPRLL-PDF] Skipped %d issues with empty summary/description", skipped)

    logger.info("[SPRLL-PDF] ✅ Done | emitted=%d | filename=%s", len(kb_rows), exact_name)
    return kb_rows

# =============================================================================
# Common transformer
# =============================================================================

def transform_sprll_issues_to_kb(
    issues: List[Dict[str, Any]],
    subsystems: List[str],
    project: str,
    filename: str,
    run_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    out: List[Dict[str, Any]] = []
    skipped = 0

    for it in issues:
        key = _safe_str(it.get("key")) or _safe_str(it.get("id"))
        fields = it.get("fields") or {}

        summary = _clean_text(_pick_field(fields, ["summary", "title"]))
        description = _clean_text(_pick_field(fields, ["description", "body"]))
        priority = _safe_name(_deep_get(fields, ["priority", "name"]))
        status = _safe_name(_deep_get(fields, ["status", "name"]))
        created = _iso_trim(_pick_field(fields, ["created", "creationDate", "createdDate"]))
        updated = _iso_trim(_pick_field(fields, ["updated", "updateDate", "modified"]))

        root_cause = _clean_text(_pick_field(fields, ["rootCause", "customfield_rootCause"]))
        corrective_action = _clean_text(_pick_field(fields, ["correctiveAction", "customfield_correctiveAction"]))

        if not summary and not description:
            skipped += 1
            continue

        text = _stitch_text(
            subsystem=subsystems,
            issue_key=key,
            summary=summary,
            description=description,
            priority=priority,
            status=status,
            created=created,
            updated=updated,
            root_cause=root_cause,
            corrective_action=corrective_action,
            max_desc_chars=600,
        )

        row = {
            "content": {
                "text": text,
                "Subsystem": subsystems,
                "IssueKey": key,
                "Summary": summary,
                "Description": description,
                "Priority": priority,
                "Status": status,
                "Created": created,
                "Updated": updated,
                "Project": project,
                "RunId": run_id or "",
                "RootCause": root_cause,
                "CorrectiveAction": corrective_action,
            },
            "metadata": {
                "subsystem": subsystems,
                "source": "sprll",
                "filename": filename,
            },
        }
        out.append(row)

    return out, skipped

# =============================================================================
# Helpers
# =============================================================================

def _compose_pull_filename(project: str, run_id: Optional[str]) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rid = f"_{run_id}" if run_id else ""
    return f"SPRLL_API_{project}_{ts}{rid}.json"

def _join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    path = (path or "").lstrip("/")
    return f"{base}/{path}"

def _build_auth_headers(auth_mode: str, pat_token: Optional[str], username: Optional[str]):
    headers = {"Accept": "application/json"}
    auth = None
    if auth_mode == "bearer" and pat_token:
        headers["Authorization"] = f"Bearer {pat_token}"
    elif auth_mode == "basic" and username and pat_token:
        import base64
        token = base64.b64encode(f"{username}:{pat_token}".encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"
    return headers, auth
