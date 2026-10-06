"""Small, stable JSON presentation contract for the native menu bar app."""
import fcntl
import json
import time

from . import codex


def worker_running(store):
    with (store.root / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def usage(value):
    limits = value["limits"]
    buckets = limits.get("rateLimitsByLimitId") or {"codex": limits.get("rateLimits") or {}}
    windows = []
    for key, bucket in sorted(buckets.items(), key=lambda item: (item[0] != "codex", item[0])):
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not window:
                continue
            minutes = window.get("windowDurationMins")
            title = {300: "5-hour", 10080: "Weekly"}.get(minutes, f"{minutes} minute" if minutes else slot.title())
            percent = window.get("usedPercent")
            windows.append({"id": f"{key}-{slot}", "bucket": bucket.get("limitName") or bucket.get("normalModelSlug") or key,
                            "title": title, "remaining": None if percent is None else max(0, min(100, 100 - percent)),
                            "resetsAt": window.get("resetsAt")})
    credits = limits.get("rateLimitResetCredits") or {}
    details = [{"id": item["id"], "title": item.get("title") or "Usage reset",
                "expiresAt": item.get("expiresAt"), "description": item.get("description") or ""}
               for item in credits.get("credits") or [] if item.get("status") == "available"]
    return {"plan": value["plan"], "windows": windows, "bankedResets": credits.get("availableCount"),
            "resetDetails": details}


def dashboard(store, binary):
    rows = store.jobs()
    active = [r for r in rows if r["status"] in ("pending", "running")]
    recent = sorted([r for r in rows if r["status"] not in ("pending", "running")],
                    key=lambda r: r["finished"] or r["created"], reverse=True)[:10]
    jobs = []
    for row in active + recent:
        spec = json.loads(row["spec"])
        jobs.append({"id": row["id"], "prompt": spec["prompt"], "status": row["status"],
                     "due": row["due"], "cwd": spec.get("cwd"), "note": row["note"],
                     "destination": spec.get("destination", "local"),
                     "cloudEnv": spec.get("cloud_env"), "cloudUrl": row.get("cloud_url"),
                     "window": spec.get("window"), "timezone": spec.get("timezone"),
                     "runs": str(store.root / "runs" / row["id"]),
                     "model": spec.get("model"), "effort": spec.get("effort")})
    result = {"jobs": jobs, "workerRunning": worker_running(store), "stateDir": str(store.root),
              "checkedAt": time.time(), "usage": None, "usageError": None}
    try:
        result["usage"] = usage(codex.snapshot(codex.executable(binary)))
    except (codex.CodexError, OSError, ValueError) as error:
        result["usageError"] = str(error)
    return result
