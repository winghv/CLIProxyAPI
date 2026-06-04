#!/usr/bin/env python3
"""CPA upstream health monitor and priority adjuster.
Runs every 60s via cron. No CPA code changes required.

Data sources:
  - usage-queue (real traffic) → degradation detection
  - direct upstream probe (chat/completions) → recovery detection
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

# ── Config loading ─────────────────────────────────────
# Secrets (upstream keys, management password) and deployment paths live in an
# external JSON config so this script can be committed to a public repo without
# leaking credentials. See health_priority.config.example.json for the schema.
# Override the config path with HEALTH_PRIORITY_CONFIG if needed.
CONFIG_FILE = os.environ.get(
    "HEALTH_PRIORITY_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_priority.config.json"),
)


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.stderr.write(
            f"FATAL: config not found at {CONFIG_FILE}\n"
            "Copy health_priority.config.example.json and fill in real values.\n"
        )
        sys.exit(2)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"FATAL: invalid JSON in {CONFIG_FILE}: {e}\n")
        sys.exit(2)


_CFG = _load_config()

# ── Constants ──────────────────────────────────────────
CONFIG_PATH = _CFG["config_path"]
STATE_PATH = _CFG["state_path"]
MGMT_PASSWORD = _CFG["mgmt_password"]
USAGE_URL = _CFG["usage_url"]

# Tunable thresholds (non-secret). Config may override via "thresholds": {...}.
_TH = _CFG.get("thresholds", {})
WINDOW_S = _TH.get("window_s", 180)
PROBE_TIMEOUT = _TH.get("probe_timeout", 30)
ERR_THRESHOLD = _TH.get("err_threshold", 0.30)
LAT_THRESHOLD_MS = _TH.get("lat_threshold_ms", 28000)
MIN_SAMPLES = _TH.get("min_samples", 5)
RECOVERY_CONSEC = _TH.get("recovery_consec", 3)
COOLDOWN_MINUTES = _TH.get("cooldown_minutes", 5)

PROVIDERS = _CFG["providers"]

KEY_TO_NAME = {v["key"]: k for k, v in PROVIDERS.items()}
URL_TO_NAME = {v["url"]: k for k, v in PROVIDERS.items()}


# ── Helpers ────────────────────────────────────────────
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # Fresh state
    state = {"providers": {}}
    for name, cfg in PROVIDERS.items():
        state["providers"][name] = {
            "events": [],
            "current_priority": cfg["ideal"],
            "degraded_at": None,
            "consecutive_ok": 0,
        }
    return state


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.rename(tmp, STATE_PATH)


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


# ── Usage queue ────────────────────────────────────────
def fetch_events() -> list[dict]:
    """Fetch raw usage events from CPA management endpoint."""
    req = urllib.request.Request(USAGE_URL)
    req.add_header("Authorization", f"Bearer {MGMT_PASSWORD}")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode("utf-8")
        if not body or body == "null":
            return []
        return json.loads(body)
    except Exception as e:
        log(f"ERROR fetching usage-queue: {e}")
        return []


def merge_events(state: dict) -> None:
    """Append new events, trim to window, reset consecutive_ok if active."""
    events = fetch_events()
    cutoff = now_ts() - WINDOW_S
    for name in PROVIDERS:
        prov = state["providers"][name]
        # Append new events for this provider
        for evt in events:
            src = evt.get("source", "")
            if KEY_TO_NAME.get(src) == name:
                prov["events"].append(evt)
        # Trim old events
        prov["events"] = [
            e for e in prov["events"]
            if _evt_ts(e) > cutoff
        ]


def _evt_ts(evt: dict) -> float:
    """Parse event timestamp to epoch float."""
    ts = evt.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ── Aggregation ────────────────────────────────────────
def aggregate(name: str, state: dict) -> tuple[int, float, int]:
    """Return (count, error_rate, avg_latency_ms) for provider's window."""
    prov = state["providers"][name]
    events = prov["events"]
    if not events:
        return 0, 0.0, 0.0
    total = len(events)
    failed = sum(1 for e in events if e.get("failed", False))
    latencies = [e.get("latency_ms", 0) for e in events]
    avg_lat = sum(latencies) / total if total > 0 else 0.0
    err_rate = failed / total if total > 0 else 0.0
    return total, err_rate, avg_lat


# ── Degradation ────────────────────────────────────────
def check_degrade(name: str, state: dict) -> None:
    """Check if an active provider should be degraded."""
    prov = state["providers"][name]
    if prov["current_priority"] <= 1:
        return  # already degraded

    count, err_rate, avg_lat = aggregate(name, state)
    if count < MIN_SAMPLES:
        return  # not enough data

    degraded = False
    reason = ""
    if err_rate > ERR_THRESHOLD:
        degraded = True
        reason = f"error_rate={err_rate:.0%}"
    elif avg_lat > LAT_THRESHOLD_MS:
        degraded = True
        reason = f"avg_lat={avg_lat/1000:.1f}s"

    if degraded:
        prov["current_priority"] = 1
        prov["degraded_at"] = now_ts()
        prov["consecutive_ok"] = 0
        log(f"{name}: {count}req {reason} ✗ 降权 {PROVIDERS[name]['ideal']}→1")


# ── Recovery probe ─────────────────────────────────────
def probe_upstream(name: str) -> tuple[bool, float]:
    """Direct inference probe to upstream API. Returns (ok, latency_ms)."""
    cfg = PROVIDERS[name]
    url = f"{cfg['url'].rstrip('/')}/chat/completions"
    body = json.dumps({
        "model": "gpt-5.4-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body)
    req.add_header("Authorization", f"Bearer {cfg['key']}")
    req.add_header("Content-Type", "application/json")

    start = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=PROBE_TIMEOUT)
        latency_ms = (time.monotonic() - start) * 1000
        resp.read()  # consume body
        ok = (resp.status == 200 and latency_ms < LAT_THRESHOLD_MS)
        return ok, latency_ms
    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        return False, latency_ms


def check_recover(name: str, state: dict) -> None:
    """Check if a degraded provider should be restored."""
    prov = state["providers"][name]
    if prov["current_priority"] > 1:
        return  # not degraded

    ok, lat = probe_upstream(name)
    if ok:
        prov["consecutive_ok"] += 1
    else:
        prov["consecutive_ok"] = 0
        prov["degraded_at"] = now_ts()  # reset cooldown
        log(f"{name}: 探活失败 lat={lat/1000:.1f}s 重置计数")
        return

    degraded_at = prov.get("degraded_at")
    cooldown_ok = True
    if degraded_at:
        cooldown_ok = (now_ts() - degraded_at) >= COOLDOWN_MINUTES * 60

    if prov["consecutive_ok"] >= RECOVERY_CONSEC and cooldown_ok:
        ideal = PROVIDERS[name]["ideal"]
        prov["current_priority"] = ideal
        prov["degraded_at"] = None
        prov["consecutive_ok"] = 0
        log(f"{name}: 探活 ok x{RECOVERY_CONSEC} 恢复 1→{ideal}")
    else:
        remaining = RECOVERY_CONSEC - prov["consecutive_ok"]
        if not cooldown_ok and degraded_at:
            remaining_cool = int(COOLDOWN_MINUTES * 60 - (now_ts() - degraded_at))
            log(f"{name}: 探活 ok({prov['consecutive_ok']}/{RECOVERY_CONSEC}) 冷却中({remaining_cool}s)")
        else:
            log(f"{name}: 探活 ok({prov['consecutive_ok']}/{RECOVERY_CONSEC})")


# ── Config update ──────────────────────────────────────
def update_config_priorities(state: dict) -> bool:
    """Write current_priority values into config.yaml. Returns True if changed."""
    import yaml  # lazy import (only needed here)
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    changed = False
    for entry in config.get("codex-api-key", []):
        base_url = entry.get("base-url", "")
        name = URL_TO_NAME.get(base_url)
        if not name:
            continue
        target = state["providers"][name]["current_priority"]
        current = entry.get("priority", 0)
        if current != target:
            entry["priority"] = target
            changed = True

    if changed:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return changed


# ── Global check ───────────────────────────────────────
def all_providers_dead(state: dict) -> bool:
    """If all providers have 0 events, skip changes (likely VPS network issue)."""
    for name in PROVIDERS:
        if state["providers"][name]["events"]:
            return False
    return True


# ── Main ───────────────────────────────────────────────
def main():
    state = load_state()

    # 1. Fetch & merge usage events
    merge_events(state)

    # 2. Check degradation (active providers)
    for name in PROVIDERS:
        check_degrade(name, state)

    # 3. Check recovery (degraded providers)
    for name in PROVIDERS:
        check_recover(name, state)

    # 4. Global protection
    if all_providers_dead(state):
        log("所有供应商无流量，跳过变更（可能VPS断网）")
        save_state(state)
        return

    # 5. Apply changes
    changed = update_config_priorities(state)
    if changed:
        log("config.yaml 已更新（热加载生效）")
    else:
        # Print summary
        parts = []
        for name in PROVIDERS:
            prov = state["providers"][name]
            count, err_rate, avg_lat = aggregate(name, state)
            if prov["current_priority"] <= 1:
                parts.append(f"{name}: 降权中")
            elif count >= MIN_SAMPLES:
                parts.append(f"{name}: {count}req err={err_rate:.0%} lat={avg_lat/1000:.1f}s ✓")
            else:
                parts.append(f"{name}: {count}req")
        log(" | ".join(parts) + " | 变更:无")

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"PANIC: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
