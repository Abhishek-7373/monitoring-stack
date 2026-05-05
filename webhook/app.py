import os
import logging
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone

# -----------------------------------------------------------
# SETUP
# -----------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

GOOGLE_CHAT_WEBHOOK = os.getenv("GOOGLE_CHAT_WEBHOOK")
HOST_NAME = os.getenv("HOST_NAME", "docker-host")

if not GOOGLE_CHAT_WEBHOOK:
    raise RuntimeError("GOOGLE_CHAT_WEBHOOK environment variable is not set")


# -----------------------------------------------------------
# HELPERS
# -----------------------------------------------------------
SEVERITY_EMOJI = {
    "critical": "🔴",
    "warning":  "🟡",
    "info":     "🔵",
}

STATUS_EMOJI = {
    "firing":   "🚨",
    "resolved": "✅",
}

SUGGESTED_ACTIONS = {
    "HighCPUUsage":        "→ SSH to host: `top -b -n1 | head -20`\n→ Check containers: `docker stats --no-stream`",
    "CriticalCPUUsage":    "→ URGENT: Identify runaway process immediately\n→ `docker stats --no-stream` | consider restart",
    "HighMemoryUsage":     "→ `free -h` | `docker stats --no-stream`\n→ Check for memory leaks in app logs",
    "CriticalMemoryUsage": "→ URGENT: OOM risk. `docker stats --no-stream`\n→ Kill/restart memory-heavy containers NOW",
    "LowDiskSpace":        "→ `df -h` | `docker system df`\n→ `docker system prune -f` to free space",
    "CriticalDiskSpace":   "→ URGENT: `docker system prune -af`\n→ Clear logs: `find /var/log -name '*.log' -mtime +7 -delete`",
    "HostDown":            "→ Ping the host | check docker ps\n→ Verify exporter container is running",
    "ContainerDown":       "→ `docker ps -a` | `docker logs <container>`\n→ `docker start <container>` or `docker-compose up -d`",
    "ContainerHighCPU":    "→ `docker logs <container> --tail 50`\n→ Check if workload is expected or runaway",
    "ContainerHighMemory": "→ Check for memory leaks\n→ Increase container memory limit in compose file",
    "ContainerRestartLoop":"→ `docker logs --tail 100 <container>`\n→ Check exit code: `docker inspect <container>`",
    "ContainerOOMKilled":  "→ URGENT: Increase memory limit in docker-compose.yml\n→ Check for memory leaks in application code",
    "DiskIOSaturation":    "→ `iostat -x 1 5` | check heavy disk writers\n→ `docker stats --no-stream`",
}


def get_service_name(labels: dict) -> str:
    """Extract most meaningful service/container name from labels."""
    return (
        labels.get("container_label_com_docker_compose_service")
        or labels.get("name")
        or labels.get("container")
        or labels.get("container_name")
        or labels.get("job")
        or labels.get("instance")
        or "unknown-service"
    )


def format_value(annotations: dict) -> str:
    """Extract current metric value if present in description."""
    description = annotations.get("description", "")
    return description


def build_google_chat_message(alert: dict) -> dict:
    """
    Build a rich Google Chat Card v2 message for a single alert.
    Uses card format for better readability vs plain text.
    """
    labels      = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status      = alert.get("status", "firing")

    alert_name  = labels.get("alertname", "UnknownAlert")
    severity    = labels.get("severity", "critical").lower()
    service     = get_service_name(labels)
    instance    = labels.get("instance", HOST_NAME)
    summary     = annotations.get("summary", "Alert triggered")
    description = annotations.get("description", "No details available.")
    runbook     = annotations.get("runbook", SUGGESTED_ACTIONS.get(alert_name, "→ Check logs and system health"))

    sev_emoji    = SEVERITY_EMOJI.get(severity, "⚠️")
    status_emoji = STATUS_EMOJI.get(status, "🚨")
    timestamp    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Resolved alert gets a clean, shorter message
    if status == "resolved":
        return {
            "text": (
                f"✅ *RESOLVED* — {summary}\n"
                f"Service: `{service}` | Host: `{instance}`\n"
                f"Resolved at: {timestamp}"
            )
        }

    # Firing alert — rich card format
    header = f"{status_emoji} {sev_emoji} [{severity.upper()}] {summary}"

    body = (
        f"*Alert:* `{alert_name}`\n"
        f"*Service:* `{service}`\n"
        f"*Host:* `{instance}`\n"
        f"*Severity:* `{severity.upper()}`\n"
        f"*Status:* `{status.upper()}`\n"
        f"*Time:* {timestamp}\n"
        f"\n"
        f"*📋 Details:*\n{description}\n"
        f"\n"
        f"*🔧 Suggested Actions:*\n{runbook}"
    )

    return {"text": f"{header}\n\n{body}"}


# -----------------------------------------------------------
# MAIN WEBHOOK ROUTE
# -----------------------------------------------------------
@app.route("/", methods=["POST"])
def receive_alert():
    """
    Receives Alertmanager webhook payload.
    Alertmanager sends: { "alerts": [...], "status": "firing"|"resolved", ... }
    """
    if not request.is_json:
        logger.warning("Received non-JSON request")
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json(silent=True)
    if not data:
        logger.warning("Empty or malformed JSON received")
        return jsonify({"error": "Empty payload"}), 400

    alerts = data.get("alerts", [])
    if not alerts:
        logger.info("Received webhook with no alerts — skipping")
        return jsonify({"status": "no_alerts"}), 200

    logger.info(f"Received {len(alerts)} alert(s) from Alertmanager")

    results = []
    for alert in alerts:
        alert_name = alert.get("labels", {}).get("alertname", "unknown")
        status     = alert.get("status", "firing")
        logger.info(f"Processing alert: {alert_name} [{status}]")

        message = build_google_chat_message(alert)

        try:
            resp = requests.post(
                GOOGLE_CHAT_WEBHOOK,
                json=message,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            resp.raise_for_status()
            logger.info(f"Sent to Google Chat: HTTP {resp.status_code} | alert={alert_name}")
            results.append({"alert": alert_name, "status": "sent", "http": resp.status_code})
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send alert {alert_name} to Google Chat: {e}")
            results.append({"alert": alert_name, "status": "failed", "error": str(e)})

    return jsonify({"processed": len(alerts), "results": results}), 200


# -----------------------------------------------------------
# HEALTH CHECK
# -----------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "host": HOST_NAME}), 200


# -----------------------------------------------------------
# DEBUG — echo last received payload (dev only, remove in prod)
# -----------------------------------------------------------
@app.route("/debug", methods=["POST"])
def debug():
    data = request.get_json(silent=True)
    logger.info(f"DEBUG payload: {data}")
    return jsonify({"received": data}), 200


# -----------------------------------------------------------
# ENTRYPOINT (dev only — prod uses gunicorn via Dockerfile)
# -----------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
