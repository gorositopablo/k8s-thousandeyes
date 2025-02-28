import os
import json
import requests
import yaml
import time
import logging
import threading
from kubernetes import client, config, watch
from flask import Flask
from prometheus_client import start_http_server, Counter
from tenacity import retry, wait_exponential, stop_after_attempt

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", force=True)

# Load Kubernetes Configuration (In-Cluster)
config.load_incluster_config()
k8s_client = client.CoreV1Api()

# Environment variables for ThousandEyes API
TE_API_TOKEN = os.getenv("TE_API_TOKEN")
NAMESPACE = os.getenv("NAMESPACE", "default")

# API Base URL
API_BASE_URL = "https://api.thousandeyes.com/v7"

# Headers for authentication
HEADERS = {
    "Authorization": f"Bearer {TE_API_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# Label selector for ConfigMaps
CONFIGMAP_LABEL = "app=thousandeyes-tests"

# Default Agents if not specified
DEFAULT_AGENTS = ["32"]

# Metrics
tests_created = Counter("thousandeyes_tests_created", "Total number of tests created")
tests_updated = Counter("thousandeyes_tests_updated", "Total number of tests updated")
tests_failed = Counter("thousandeyes_tests_failed", "Total number of failed test creations or updates")

# Read dry-run mode from config
def get_dry_run_from_config():
    configmaps = k8s_client.list_namespaced_config_map(namespace=NAMESPACE, label_selector=CONFIGMAP_LABEL).items
    for cm in configmaps:
        try:
            if "config.yaml" in cm.data:
                config_data = yaml.safe_load(cm.data["config.yaml"])
                return config_data.get("dryRun", False)
        except Exception as e:
            logging.error(f"Error parsing ConfigMap {cm.metadata.name}: {e}")
    return False

# Fetch ConfigMaps with test definitions
def list_configmaps():
    try:
        return k8s_client.list_namespaced_config_map(namespace=NAMESPACE, label_selector=CONFIGMAP_LABEL).items
    except Exception as e:
        logging.error(f"Failed to list ConfigMaps: {e}")
        return []

# Load test definitions
def load_configs():
    http_tests = []
    configmaps = list_configmaps()

    for cm in configmaps:
        try:
            if "config.yaml" in cm.data:
                config_data = yaml.safe_load(cm.data["config.yaml"])
                for test in config_data.get("httpTests", []):
                    # Ensure testName exists
                    test.setdefault("testName", f"Test-{test.get('url', 'unknown')}")

                    # Ensure agents exist
                    test["agents"] = [{"agentId": agent} for agent in test.get("agents", DEFAULT_AGENTS)]

                    http_tests.append(test)
        except Exception as e:
            logging.error(f"Error parsing ConfigMap {cm.metadata.name}: {e}")

    return http_tests

# Fetch existing ThousandEyes tests
def get_existing_tests():
    url = f"{API_BASE_URL}/tests/http-server"
    response = requests.get(url, headers=HEADERS)

    if response.status_code == 200:
        return response.json().get("tests", [])
    else:
        logging.error(f"Failed to fetch existing tests: {response.status_code} - {response.text}")
        return []

# Exponential backoff decorator for API requests
@retry(wait=wait_exponential(multiplier=2, min=2, max=10), stop=stop_after_attempt(5))
def create_test(test, dry_run):
    if dry_run:
        logging.info(f"[DRY RUN] Would create test: {test['testName']}")
        return

    url = f"{API_BASE_URL}/tests/http-server"
    response = requests.post(url, headers=HEADERS, json=test)

    if response.status_code == 201:
        logging.info(f"Test '{test['testName']}' created successfully.")
        tests_created.inc()
    else:
        logging.error(f"Failed to create test '{test['testName']}': {response.status_code} - {response.text}")
        tests_failed.inc()

@retry(wait=wait_exponential(multiplier=2, min=2, max=10), stop=stop_after_attempt(5))
def update_test(test_id, test, dry_run):
    if dry_run:
        logging.info(f"[DRY RUN] Would update test: {test['testName']}' (ID: {test_id})")
        return

    url = f"{API_BASE_URL}/tests/http-server/{test_id}"
    response = requests.put(url, headers=HEADERS, json=test)

    if response.status_code == 200:
        logging.info(f"Test '{test['testName']}' updated successfully.")
        tests_updated.inc()
    else:
        logging.error(f"Failed to update test '{test['testName']}': {response.status_code} - {response.text}")
        tests_failed.inc()

# Sync tests
def sync_tests():
    dry_run = get_dry_run_from_config()
    logging.info(f"Starting sync process (Dry Run: {dry_run})")

    tests = load_configs()
    existing_tests = get_existing_tests()

    for test in tests:
        existing_test = next((t for t in existing_tests if t.get("url") == test.get("url")), None)

        if existing_test:
            logging.info(f"Test '{test['testName']}' exists, updating it.")
            update_test(existing_test["testId"], test, dry_run)
        else:
            logging.info(f"Test '{test['testName']}' does not exist, creating it.")
            create_test(test, dry_run)

# Watch for ConfigMap changes with auto-reconnect
def watch_configmaps():
    while True:
        try:
            w = watch.Watch()
            logging.info("Started watching for ConfigMap changes...")
            for event in w.stream(k8s_client.list_namespaced_config_map, namespace=NAMESPACE, label_selector=CONFIGMAP_LABEL, timeout_seconds=600):
                logging.info(f"ConfigMap changed: {event['type']} - {event['object'].metadata.name}")
                sync_tests()
        except Exception as e:
            logging.error(f"ConfigMap watcher failed: {e}. Reconnecting in 10 seconds...")
            time.sleep(10)  # Wait before reconnecting

# Flask health check endpoint
app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return {"status": "healthy"}, 200

if __name__ == "__main__":
    # Start Prometheus metrics server
    start_http_server(8000)

    # Start Flask health check
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)).start()

    # Run sync_tests once on startup (Ensures logs show actual activity)
    sync_tests()

    # Start ConfigMap watcher in a separate thread
    threading.Thread(target=watch_configmaps, daemon=True).start()

    # Keep the main thread alive
    while True:
        time.sleep(60)
