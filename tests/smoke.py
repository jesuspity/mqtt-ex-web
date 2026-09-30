"""Testa a tela, a proteção de acesso, configurações e bloqueio de CSRF."""
import base64
import os
import tempfile

os.environ["MQTT_DATA_DIR"] = tempfile.mkdtemp(prefix="mqtt-web-test-")
os.environ["WEB_USER"] = "ci-user"
os.environ["WEB_PASS"] = "ci-only-test-password"

from app import app  # noqa: E402

client = app.test_client()
assert client.get("/healthz").status_code == 200
assert client.get("/").status_code == 401
basic = base64.b64encode(b"ci-user:ci-only-test-password").decode("ascii")
headers = {"Authorization": f"Basic {basic}"}
assert client.get("/", headers=headers).status_code == 200
assert client.get("/api/settings", headers=headers).get_json()["host"] == ""
assert client.post("/api/settings", json={"host": ""}, headers=headers).status_code == 400
blocked = client.post("/api/publish", json={}, headers={**headers, "Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"})
assert blocked.status_code == 403
print("Smoke checks passed.")
