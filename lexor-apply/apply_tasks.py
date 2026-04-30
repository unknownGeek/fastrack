"""
Fastrack: fetch tasks from Instahyre job_search API with pagination and
apply to each task using the candidate_opportunity/apply endpoint.

Curls are read from sibling text files (getTasks.txt, applyTask.txt) so the
user can replace them freely when tokens/cookies expire. Only the `offset`
query parameter on the get-tasks URL is modified by this script; everything
else (headers, cookies, search params, body template) is used as-is.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests


PAGE_SIZE = 35
REQUEST_DELAY_SECONDS = 1.0
APPLY_DELAY_SECONDS = 1.5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GET_TASKS_FILE = os.path.join(SCRIPT_DIR, "getTasks.txt")
APPLY_TASK_FILE = os.path.join(SCRIPT_DIR, "applyTask.txt")
APPLIED_LOG_FILE = os.path.join(SCRIPT_DIR, "applied_tasks.txt")
FAILED_LOG_FILE = os.path.join(SCRIPT_DIR, "failed_tasks.txt")


def _read_curl_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _normalize_curl(raw: str) -> str:
    """Join multi-line curls (backslash-newline) into a single line and drop
    the non-standard `curl POST` prefix if present."""
    text = raw.replace("\\\n", " ").replace("\\\r\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^curl\s+POST\b", "curl -X POST", text, flags=re.IGNORECASE)
    return text


def parse_curl(raw_curl: str) -> dict[str, Any]:
    """Parse a curl command string into method/url/headers/body.

    Supports `--header/-H`, `--data/--data-raw/-d`, the non-standard `--body`,
    `--cookie/-b`, and `-X/--request` flags. Other flags are ignored.
    """
    text = _normalize_curl(raw_curl)
    tokens = shlex.split(text)

    if not tokens or tokens[0].lower() != "curl":
        raise ValueError("Input does not look like a curl command")

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    cookies: str | None = None
    body: str | None = None

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-X", "--request"):
            method = tokens[i + 1].upper()
            i += 2
        elif tok in ("-H", "--header"):
            hv = tokens[i + 1]
            if ":" in hv:
                name, value = hv.split(":", 1)
                name = name.strip()
                value = value.strip()
                if name.lower() == "cookie":
                    cookies = value
                else:
                    headers[name] = value
            i += 2
        elif tok in ("-b", "--cookie"):
            cookies = tokens[i + 1]
            i += 2
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--body"):
            body = tokens[i + 1]
            i += 2
        elif tok in ("--compressed", "-L", "--location", "-k", "--insecure", "-s", "--silent"):
            i += 1
        elif tok.startswith("-"):
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        else:
            if url is None:
                url = tok
            i += 1

    if url is None:
        raise ValueError("No URL found in curl")

    if cookies:
        headers["Cookie"] = cookies

    if method is None:
        method = "POST" if body is not None else "GET"

    return {"method": method, "url": url, "headers": headers, "body": body}


def build_url_with_offset(base_url: str, offset: int) -> str:
    """Return the base_url with the `offset` query param overridden."""
    parsed = urlparse(base_url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params = [(k, v) for (k, v) in params if k != "offset"]
    params.append(("offset", str(offset)))
    new_query = urlencode(params)
    return urlunparse(parsed._replace(query=new_query))


def fetch_page(get_cfg: dict[str, Any], offset: int) -> dict[str, Any]:
    url = build_url_with_offset(get_cfg["url"], offset)
    resp = requests.request(
        method=get_cfg["method"],
        url=url,
        headers=get_cfg["headers"],
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def apply_to_task(apply_cfg: dict[str, Any], task_id: Any) -> tuple[bool, str]:
    try:
        body_template = json.loads(apply_cfg["body"]) if apply_cfg.get("body") else {}
    except json.JSONDecodeError:
        return False, f"Apply body is not valid JSON: {apply_cfg.get('body')!r}"

    body_template["job_id"] = int(task_id) if str(task_id).isdigit() else task_id

    resp = requests.request(
        method=apply_cfg["method"] or "POST",
        url=apply_cfg["url"],
        headers=apply_cfg["headers"],
        data=json.dumps(body_template),
        timeout=30,
    )

    ok = 200 <= resp.status_code < 300
    snippet = resp.text[:300].replace("\n", " ")
    return ok, f"HTTP {resp.status_code} {snippet}"


def _append_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def _load_applied_ids() -> set[str]:
    if not os.path.exists(APPLIED_LOG_FILE):
        return set()
    with open(APPLIED_LOG_FILE, "r", encoding="utf-8") as fh:
        return {ln.strip() for ln in fh if ln.strip()}


def main() -> int:
    if not os.path.exists(GET_TASKS_FILE):
        print(f"Missing {GET_TASKS_FILE}", file=sys.stderr)
        return 1
    if not os.path.exists(APPLY_TASK_FILE):
        print(f"Missing {APPLY_TASK_FILE}", file=sys.stderr)
        return 1

    get_cfg = parse_curl(_read_curl_file(GET_TASKS_FILE))
    apply_cfg = parse_curl(_read_curl_file(APPLY_TASK_FILE))

    print(f"[info] GET  {get_cfg['url']}")
    print(f"[info] POST {apply_cfg['url']}")

    already_applied = _load_applied_ids()
    if already_applied:
        print(f"[info] Skipping {len(already_applied)} previously applied task ids")

    offset = 0
    total_seen = 0
    total_applied = 0
    total_failed = 0

    while True:
        print(f"\n[page] offset={offset}")
        try:
            payload = fetch_page(get_cfg, offset)
        except requests.HTTPError as exc:
            print(f"[error] Failed to fetch offset={offset}: {exc} -> {exc.response.text[:300] if exc.response is not None else ''}")
            return 2
        except Exception as exc:
            print(f"[error] Failed to fetch offset={offset}: {exc}")
            return 2

        objects = payload.get("objects") or []
        if not objects:
            print("[info] No more tasks. Done.")
            break

        print(f"[page] received {len(objects)} tasks")
        total_seen += len(objects)

        for obj in objects:
            task_id = obj.get("id")
            if task_id is None:
                continue
            task_id_str = str(task_id)
            if task_id_str in already_applied:
                print(f"  - skip {task_id_str} (already applied)")
                continue

            ok, info = apply_to_task(apply_cfg, task_id)
            if ok:
                total_applied += 1
                already_applied.add(task_id_str)
                _append_line(APPLIED_LOG_FILE, task_id_str)
                print(f"  + applied {task_id_str} ({info})")
            else:
                total_failed += 1
                _append_line(FAILED_LOG_FILE, f"{task_id_str}\t{info}")
                print(f"  ! failed  {task_id_str} ({info})")

            time.sleep(APPLY_DELAY_SECONDS)

        meta = payload.get("meta") or {}
        total_count = meta.get("total_count")
        if isinstance(total_count, int) and offset + PAGE_SIZE >= total_count:
            print(f"[info] Reached total_count={total_count}. Done.")
            break

        if len(objects) < PAGE_SIZE:
            print("[info] Last page (fewer than page size). Done.")
            break

        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY_SECONDS)

    print(
        f"\n[summary] seen={total_seen} applied={total_applied} failed={total_failed} "
        f"log={APPLIED_LOG_FILE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
