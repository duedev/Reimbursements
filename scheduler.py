"""Built-in scheduled spreadsheet export.

On a configured weekly schedule the server generates the workbook into a
pre-configured export folder (point it at a Dropbox/Drive/OneDrive sync folder
for zero-config cloud upload), optionally uploads it straight to Dropbox via
the HTTP API, and optionally emails it through the existing SMTP support.

Configuration comes from env vars, overridable at runtime through the
"schedule" block of .app_config.json (edited via the web UI):

    SCHEDULE_ENABLED        0/1            (default 0)
    SCHEDULE_TIME           HH:MM          (default 17:00, local time)
    SCHEDULE_DAYS           mon,tue,...    or "daily"/"weekdays" (default thu)
    EXPORT_FOLDER           container path (default /data/export)
    SCHEDULE_DROPBOX_TOKEN  Dropbox access token (empty = no API upload)
    SCHEDULE_EMAIL          0/1            email via SMTP_* vars (default 0)
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_ALIASES = {
    "daily":    set(DAY_NAMES),
    "weekdays": {"mon", "tue", "wed", "thu", "fri"},
}

EXPORT_FOLDER = Path(os.getenv("EXPORT_FOLDER", "/data/export"))


class ScheduleError(ValueError):
    """Raised for invalid schedule configuration."""


@dataclass
class ScheduleConfig:
    enabled: bool = False
    hour: int = 17
    minute: int = 0
    days: set[str] = field(default_factory=lambda: {"thu"})
    dropbox_token: str = ""
    email: bool = False

    @property
    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def days_str(self) -> str:
        return ",".join(d for d in DAY_NAMES if d in self.days)


def _env_defaults() -> dict:
    return {
        "enabled":       os.getenv("SCHEDULE_ENABLED", "0"),
        "time":          os.getenv("SCHEDULE_TIME", "17:00"),
        "days":          os.getenv("SCHEDULE_DAYS", "thu"),
        "dropbox_token": os.getenv("SCHEDULE_DROPBOX_TOKEN", ""),
        "email":         os.getenv("SCHEDULE_EMAIL", "0"),
    }


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def parse_schedule(cfg: Optional[dict] = None) -> ScheduleConfig:
    """Build a validated ScheduleConfig from env defaults merged with cfg."""
    merged = _env_defaults()
    merged.update({k: v for k, v in (cfg or {}).items() if v is not None})

    time_str = str(merged["time"]).strip()
    try:
        hour_s, minute_s = time_str.split(":")
        hour, minute = int(hour_s), int(minute_s)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ScheduleError(f"Invalid schedule time {time_str!r} — use HH:MM (24h)")

    days_raw = str(merged["days"]).strip().lower()
    if days_raw in DAY_ALIASES:
        days = set(DAY_ALIASES[days_raw])
    else:
        days = {d.strip()[:3] for d in days_raw.split(",") if d.strip()}
        unknown = days - set(DAY_NAMES)
        if unknown or not days:
            raise ScheduleError(
                f"Invalid schedule days {days_raw!r} — use e.g. 'thu', 'mon,fri', "
                "'daily' or 'weekdays'")

    return ScheduleConfig(
        enabled=_as_bool(merged["enabled"]),
        hour=hour, minute=minute, days=days,
        dropbox_token=str(merged["dropbox_token"]).strip(),
        email=_as_bool(merged["email"]),
    )


def next_run(cfg: ScheduleConfig, now: datetime) -> Optional[datetime]:
    """Next datetime the schedule fires at, or None when disabled."""
    if not cfg.enabled or not cfg.days:
        return None
    for offset in range(8):
        candidate = (now + timedelta(days=offset)).replace(
            hour=cfg.hour, minute=cfg.minute, second=0, microsecond=0)
        if DAY_NAMES[candidate.weekday()] in cfg.days and candidate > now:
            return candidate
    return None


def upload_dropbox(path: Path, token: str) -> None:
    """Upload a file to the Dropbox app folder root via the HTTP API."""
    api_arg = json.dumps({
        "path": f"/{path.name}",
        "mode": "overwrite",
        "autorename": False,
        "mute": True,
    })
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/upload",
        data=path.read_bytes(),
        method="POST",
        headers={
            "Authorization":   f"Bearer {token}",
            "Dropbox-API-Arg": api_arg,
            "Content-Type":    "application/octet-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def run_export(cfg: ScheduleConfig, results: list[dict], employee: str,
               export_dir: Path = EXPORT_FOLDER) -> dict:
    """Generate the workbook into export_dir and deliver it. Synchronous."""
    from process_receipts import generate_spreadsheet  # late import: heavy module
    from watch_mode import send_workbook_email

    if not results:
        return {"ok": False, "error": "No completed receipts to export"}

    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = generate_spreadsheet(results, export_dir, employee)
    if not out_path:
        return {"ok": False, "error": "Spreadsheet generation failed"}
    out_path = Path(out_path)

    report = {"ok": True, "filename": out_path.name, "delivered": ["folder"]}
    if cfg.dropbox_token:
        try:
            upload_dropbox(out_path, cfg.dropbox_token)
            report["delivered"].append("dropbox")
        except Exception as exc:
            report["dropbox_error"] = str(exc)
    if cfg.email:
        mail = send_workbook_email(out_path, len(results))
        if mail.get("ok"):
            report["delivered"].append("email")
        else:
            report["email_error"] = mail.get("error", "unknown")
    return report


async def run_scheduler(
    get_schedule: Callable[[], ScheduleConfig],
    get_results: Callable[[], tuple[list[dict], str]],
    on_result: Callable[[dict], None],
    wakeup: asyncio.Event,
) -> None:
    """Background task: sleep until the next firing time, export, repeat.

    get_schedule  → current (possibly UI-edited) ScheduleConfig
    get_results   → (completed results snapshot, employee name)
    on_result     → called with the export report (for status + SSE log)
    wakeup        → set by the /schedule endpoint to recompute immediately
    """
    while True:
        try:
            cfg = get_schedule()
            target = next_run(cfg, datetime.now())
            if target is None:
                # Disabled — wait until config changes (poll hourly as backstop)
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    pass
                wakeup.clear()
                continue

            wait_s = (target - datetime.now()).total_seconds()
            if wait_s > 0:
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=min(wait_s, 60))
                    wakeup.clear()
                    continue  # config changed — recompute target
                except asyncio.TimeoutError:
                    if datetime.now() < target:
                        continue  # not there yet — keep sleeping in ≤60s slices

            results, employee = get_results()
            report = await asyncio.get_event_loop().run_in_executor(
                None, run_export, cfg, results, employee)
            report["ran_at"] = datetime.now().isoformat(timespec="seconds")
            on_result(report)
            # Avoid double-firing within the same minute
            await asyncio.sleep(61)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                on_result({"ok": False, "error": f"Scheduler error: {exc}"})
            except Exception:
                pass
            await asyncio.sleep(60)
