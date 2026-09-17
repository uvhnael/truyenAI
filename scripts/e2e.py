#!/usr/bin/env python3
"""Real HTTP E2E; requires API, PostgreSQL, Redis and worker already running.

Run scripts/e2e.sh (API_URL defaults to http://localhost:18080).
LIVE is refused before any write unless --allow-live explicitly permits charges.
The report contains observed IDs/counts/hashes, never credentials or raw errors.
"""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / ".artifacts" / "e2e.json"
DEMO_MARKER = "[BẢN DEMO"


class CheckFailed(RuntimeError):
    pass


def require(condition, message):
    # Unlike assert, these checks are retained under python -O.
    if not condition:
        raise CheckFailed(message)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class API:
    def __init__(self, url, report):
        parsed = urlsplit(url.rstrip("/"))
        require(parsed.scheme in ("http", "https") and parsed.hostname, "Invalid API_URL")
        require(not (parsed.username or parsed.password or parsed.query or parsed.fragment),
                "API_URL must not contain credentials, query or fragment")
        self.url = parsed
        self.report = report
        report["api_url"] = parsed.geturl()

    def connect(self, timeout):
        cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        return cls(self.url.hostname, self.url.port, timeout=timeout)

    def request(self, method, path, payload=None, expected=200, timeout=10, text=False):
        connection = self.connect(timeout)
        headers = {"Accept": "text/plain" if text else "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, self.url.path + path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(10_000_001)
            self.report["requests"].append({"method": method, "path": path, "status": response.status})
            require(response.status == expected,
                    f"{method} {path}: expected HTTP {expected}, got {response.status}")
            require(len(raw) <= 10_000_000, "Response exceeded 10 MB safety limit")
            data = raw.decode("utf-8")
            return (data if text else json.loads(data)), dict(response.getheaders())
        finally:
            connection.close()

    def json(self, method, path, payload=None, expected=200, timeout=10):
        return self.request(method, path, payload, expected, timeout)[0]

    def event(self, path, cursor=None):
        """Read exactly one dispatched SSE event, then close the infinite stream."""
        deadline = time.monotonic() + 10
        connection = self.connect(10)
        headers = {"Accept": "text/event-stream"}
        if cursor is not None:
            headers["Last-Event-ID"] = str(cursor)
        try:
            connection.request("GET", self.url.path + path, headers=headers)
            response = connection.getresponse()
            self.report["requests"].append({"method": "GET", "path": path, "status": response.status,
                                            "last_event_id": cursor, "stream": True})
            require(response.status == 200, "SSE did not return HTTP 200")
            require("text/event-stream" in response.getheader("Content-Type", ""),
                    "SSE Content-Type is incorrect")
            event_id, data, consumed = None, [], 0
            while time.monotonic() < deadline:
                if connection.sock is not None:
                    connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
                raw = response.readline(65537)
                require(raw, "SSE closed without an event")
                consumed += len(raw)
                require(consumed <= 1_000_000 and len(raw) <= 65536, "SSE event exceeded safety limit")
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        if event_id is None:
                            raise CheckFailed("SSE event lacks cursor ID")
                        return {"id": int(event_id), "data": json.loads("\n".join(data))}
                    event_id, data = None, []
                elif line.startswith("id:"):
                    event_id = line[3:].lstrip(" ")
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
            raise CheckFailed("Timed out waiting for an SSE event")
        finally:
            # Do not response.read(): this endpoint intentionally never reaches EOF.
            connection.close()


def run(api, report, allow_live):
    health = api.json("GET", "/api/health")
    report["health"] = {key: health.get(key) for key in
                        ("status", "database", "redis", "worker", "llm_mode")}
    mode = health.get("llm_mode")
    require(mode in ("mock", "live"), "Health returned an unknown LLM mode; refusing writes")
    require(mode == "mock" or allow_live, "LIVE mode refused: pass --allow-live to permit LLM charges")
    require(health.get("status") == "ok" and health.get("database") == "ok"
            and health.get("redis") is True and health.get("worker") is True,
            "API health is not ready (database, Redis and worker must all be healthy)")
    report["checks"].append("healthy_dependencies_and_explicit_llm_mode")

    # Validate failures before creating a project; these calls must not enqueue work.
    invalid = [
        {"title": "E2E invalid", "prompt": "short"},
        {"title": "E2E invalid", "prompt": "Một hành trình trở về làng.", "options": {"chapter_count": 0}},
        {"title": "E2E invalid", "prompt": "Một hành trình trở về làng.", "options": {"chapter_count": 31}},
        {"title": " ", "prompt": "Một hành trình trở về làng."},
    ]
    for payload in invalid:
        api.json("POST", "/api/projects", payload, expected=422)
    missing_id = str(uuid4())
    api.json("GET", f"/api/projects/{missing_id}", expected=404)
    report["negative_tests"] = {"invalid_create_422": len(invalid), "missing_project_404": missing_id}
    report["checks"].extend(["invalid_create_422", "missing_project_404"])

    created = api.json("POST", "/api/projects", {
        "title": f"E2E — Ngọn đèn trở về {uuid4().hex[:8]}",
        "prompt": "Viết truyện tiếng Việt gồm 3 chương về An và chiếc đèn nói thật. "
                  "An vượt qua nỗi sợ, tìm nguồn nước và trở về làng. Kết thúc có hậu.",
        "options": {"chapter_count": 3, "target_words": 200},
    }, expected=201)
    project_id = created["id"]
    # IDs are server-generated UUIDs; validate before putting them into URL paths.
    from uuid import UUID
    UUID(project_id)
    report["project_id"] = project_id
    base = f"/api/projects/{project_id}"
    started = time.monotonic()
    deadline = started + 120
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0, "Project did not complete within 120 seconds")
        project = api.json("GET", base, timeout=min(10, remaining))
        job = project.get("job") or {}
        report["last_observed"] = {"project_status": project.get("status"),
                                   "job_status": job.get("status"), "stage": job.get("stage")}
        require(project.get("status") not in ("failed", "cancelled"), "Project failed or was cancelled")
        if job.get("status") == "done":
            require(project.get("status") == "completed", "Finished project still needs review")
            break
        require(job.get("status") in ("queued", "running"), "Unexpected job status")
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    report["generation_seconds"] = round(time.monotonic() - started, 3)
    report["checks"].append("three_chapter_project_completed_within_120s")

    require(project["spec"]["chapter_count"] == 3 and project["spec"]["target_words"] == 200,
            "Explicit project options were not preserved")
    bible = project.get("bible") or {}
    require(bible.get("world") and bible.get("facts") and bible.get("characters"), "Story bible incomplete")
    characters = project.get("characters", [])
    require(characters and sorted(c["name"] for c in characters)
            == sorted(c["name"] for c in bible["characters"]), "Persisted characters differ from bible")
    for character in characters:
        require(character["project_id"] == project_id and character["description"] and character["arc"],
                "Persisted character incomplete")
    outline = project.get("outline", [])
    chapters = project.get("chapters", [])
    require([o["chapter_number"] for o in outline] == [1, 2, 3], "Outline is not exactly three chapters")
    require(all(o["title"] and o["summary"] and o["beats"] for o in outline), "Outline incomplete")
    require([c["chapter_number"] for c in chapters] == [1, 2, 3], "Chapters are not numbered 1–3")
    require(project["final_review"]["verdict"] == "pass", "Final review did not pass")
    require(api.json("GET", base + "/chapters") == chapters, "Chapter list differs from project detail")
    report["counts"] = {"characters": len(characters), "outline": len(outline), "chapters": len(chapters)}
    report["chapters"] = []
    originals = {}
    for chapter in chapters:
        n = chapter["chapter_number"]
        require(chapter["content"].strip() and chapter["summary"] and chapter["memory"],
                f"Chapter {n} content or memory missing")
        require(chapter["word_count"] == len(chapter["content"].split()), f"Chapter {n} word count incorrect")
        require(chapter["status"] == "final" and chapter["version"] >= 1, f"Chapter {n} is not final")
        require(api.json("GET", f"{base}/chapters/{n}") == chapter, f"Chapter {n} read differs")
        revisions = api.json("GET", f"{base}/chapters/{n}/revisions")
        require([r["revision_no"] for r in revisions] == list(range(chapter["version"], 0, -1)),
                f"Chapter {n} revisions are not complete and descending")
        require(revisions[0]["content"] == chapter["content"], f"Chapter {n} current revision differs")
        require(revisions[-1]["source"] == "draft" and revisions[-1]["content"].strip(),
                f"Chapter {n} original draft missing")
        if mode == "mock":
            require(DEMO_MARKER in chapter["content"], f"Chapter {n} lacks explicit demo marker")
            require(any(r["source"] == "ai_revision" for r in revisions),
                    f"Chapter {n} mock workflow did not exercise AI revision")
        originals[n] = revisions
        report["chapters"].append({"number": n, "id": chapter["id"], "version": chapter["version"],
                                    "word_count": chapter["word_count"], "content_sha256": digest(chapter["content"]),
                                    "revision_count": len(revisions),
                                    "revision_sources": [r["source"] for r in revisions]})
    report["counts"]["revisions_before_edit"] = sum(len(rows) for rows in originals.values())
    report["checks"].append("persisted_spec_bible_characters_outline_chapters_and_revisions")

    exported, headers = api.request("GET", base + "/export", text=True)
    expected_export = f"# {project['title']}\n\n" + "\n\n".join(
        f"## Chương {c['chapter_number']}: {c['title']}\n\n{c['content']}" for c in chapters)
    require(exported == expected_export, "Export differs from persisted title/chapters")
    require("text/plain" in headers.get("content-type", ""), "Export MIME type is incorrect")
    require(f"story-{project_id}.md" in headers.get("content-disposition", ""), "Export filename missing")
    report["export"] = {"bytes": len(exported.encode("utf-8")), "sha256": digest(exported),
                         "content_disposition": headers.get("content-disposition")}
    report["checks"].append("markdown_export_exact_content")

    first = api.event(base + "/events")
    second = api.event(base + "/events", cursor=first["id"])
    query_replay = api.event(base + f"/events?after={first['id']}")
    require(first["data"]["type"] == "queued", "SSE did not replay the first persisted queued event")
    require(second["id"] > first["id"], "Last-Event-ID replay duplicated cursor or moved backward")
    require(second == query_replay, "Query cursor and Last-Event-ID replay disagree")
    for item in (first, second):
        require(item["data"]["project_id"] == project_id and item["data"]["id"] == item["id"],
                "SSE returned an event from a different project or inconsistent ID")
    report["sse"] = {"first_id": first["id"], "first_type": first["data"]["type"],
                      "resumed_id": second["id"], "resumed_type": second["data"]["type"],
                      "query_replay_id": query_replay["id"], "events_per_connection": 1}
    report["checks"].append("sse_one_event_close_and_cursor_replay")

    chapter = chapters[0]
    edit_path = base + "/chapters/1"
    edited_content = chapter["content"] + "\n\nGhi chú E2E: An giữ lời hứa với ngôi làng."
    edit = {"title": chapter["title"] + " — đã sửa", "content": edited_content, "version": chapter["version"]}
    saved = api.json("PUT", edit_path, edit)
    require(saved["version"] == chapter["version"] + 1, "Manual edit did not increment version exactly once")
    require(saved["content"] == edited_content and saved["title"] == edit["title"]
            and saved["status"] == "edited", "Manual edit content/title/status mismatch")
    require(saved["summary"] == "" and saved["memory"] == {}, "Manual edit left stale chapter memory")
    require(saved["word_count"] == len(edited_content.split()), "Edited chapter word count incorrect")
    after = api.json("GET", edit_path + "/revisions")
    require(len(after) == len(originals[1]) + 1 and after[1:] == originals[1],
            "Manual edit did not preserve every previous revision including original draft")
    require(after[0]["revision_no"] == saved["version"] and after[0]["source"] == "manual"
            and after[0]["content"] == edited_content and after[0]["title"] == edit["title"],
            "Manual revision does not match saved chapter")
    require(api.json("GET", edit_path) == saved, "Manual edit did not persist")
    updated = api.json("GET", base)
    require(updated["status"] == "needs_review" and updated["final_review"]["verdict"] == "revise"
            and updated["job"]["status"] == "done", "Manual edit did not invalidate old quality assurance")
    report["manual_edit"] = {"old_version": chapter["version"], "new_version": saved["version"],
                              "revision_count": len(after), "original_draft_id": after[-1]["id"],
                              "original_draft_sha256": digest(after[-1]["content"]),
                              "content_sha256": digest(saved["content"]), "project_status": updated["status"]}
    report["counts"]["revisions_after_edit"] = sum(len(rows) for rows in originals.values()) + 1
    report["checks"].append("manual_save_increments_version_and_preserves_original_draft")

    api.json("PUT", edit_path, edit, expected=409)
    api.json("PUT", edit_path, {**edit, "version": saved["version"], "content": " "}, expected=422)
    api.json("POST", base + "/run", expected=409)
    require(api.json("GET", edit_path) == saved, "Rejected requests modified chapter")
    require(api.json("GET", edit_path + "/revisions") == after, "Rejected requests modified revisions")
    report["negative_tests"].update({"stale_version": 409, "invalid_edit": 422, "completed_rerun": 409})
    report["checks"].extend(["stale_version_409", "invalid_edit_422", "completed_rerun_409", "rejections_no_mutation"])
    final_export, _ = api.request("GET", base + "/export", text=True)
    require(edited_content in final_export and f"## Chương 1: {edit['title']}" in final_export,
            "Export does not include the manual edit")
    report["export_after_edit"] = {"bytes": len(final_export.encode("utf-8")), "sha256": digest(final_export)}
    report["checks"].append("export_reflects_manual_edit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true", help="Permit live LLM calls and associated charges")
    args = parser.parse_args()
    report = {"status": "running", "started_at": utc_now(), "allow_live": args.allow_live,
              "project_id": None, "checks": [], "requests": []}
    started = time.monotonic()
    code = 1
    try:
        api = API(os.environ.get("API_URL", "http://localhost:18080"), report)
        run(api, report, args.allow_live)
        report["status"] = "passed"
        code = 0
    except (Exception, KeyboardInterrupt) as exc:
        report["status"] = "failed"
        # Provider/server bodies and network errors may contain secrets; never persist them.
        report["error"] = {"type": type(exc).__name__,
                           "message": str(exc) if isinstance(exc, CheckFailed) else
                           "Execution failed; check API availability, response contracts and server logs."}
    finally:
        report["finished_at"] = utc_now()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "project_id": report["project_id"],
                      "checks_passed": len(report["checks"]), "report": str(REPORT_PATH),
                      **({"error": report["error"]} if "error" in report else {})}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
