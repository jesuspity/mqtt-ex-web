"""Minimal MQTT web panel. No broker or Telegram credentials are included in source."""
import atexit
import hmac
import json
import os
import ssl
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
import paho.mqtt.client as mqtt

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("MQTT_DATA_DIR", str(BASE_DIR / "data"))).resolve()
SETTINGS_FILE = DATA_DIR / "settings.json"
WEB_USER = os.getenv("WEB_USER", "").strip()
WEB_PASS = os.getenv("WEB_PASS", "")
PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))

app = Flask(__name__)
lock = threading.RLock()
messages = deque(maxlen=200)
state = {"config": {}, "client": None, "connected": False, "error": "Configure o broker MQTT nas configurações."}


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_settings(config):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = DATA_DIR / f".settings-{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(config, file, ensure_ascii=False)
        os.replace(temp, SETTINGS_FILE)
        os.chmod(SETTINGS_FILE, 0o600)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def check_auth(username, password):
    if not WEB_USER or not WEB_PASS or not isinstance(username, str) or not isinstance(password, str):
        return False
    return hmac.compare_digest(username.encode(), WEB_USER.encode()) and hmac.compare_digest(password.encode(), WEB_PASS.encode())


def auth_required():
    return Response("Acesso restrito.", 401, {"WWW-Authenticate": 'Basic realm="MQTT Web"'})


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.before_request
def protect_app():
    if request.endpoint == "healthz":
        return None
    if not WEB_USER or not WEB_PASS:
        return Response("Configure WEB_USER e WEB_PASS no serviço de hospedagem.", 503)
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return auth_required()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return jsonify(error="Origem não permitida."), 403
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != request.host.lower():
            return jsonify(error="Origem não permitida."), 403


def safe_settings(config=None):
    config = config or state["config"]
    return {
        "host": config.get("host", ""), "port": config.get("port", 8883),
        "username": config.get("username", ""), "topic": config.get("topic", "#"),
        "tls": bool(config.get("tls", True)),
        "mqtt_password_saved": bool(config.get("password")),
        "telegram_configured": bool(config.get("telegram_token") and config.get("telegram_chat_id")),
    }


def stop_mqtt():
    with lock:
        client = state["client"]
        state["client"] = None
        state["connected"] = False
    if client:
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass


def connect_mqtt(config):
    stop_mqtt()
    if not config.get("host"):
        state["error"] = "Configure o host do broker MQTT nas configurações."
        return
    try:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"mqtt-web-{uuid.uuid4().hex[:10]}")
        except AttributeError:
            client = mqtt.Client(client_id=f"mqtt-web-{uuid.uuid4().hex[:10]}")
        if config.get("username"):
            client.username_pw_set(config["username"], config.get("password", ""))
        if config.get("tls", True):
            client.tls_set_context(ssl.create_default_context())

        def on_connect(client, _userdata, _flags, reason_code, *_args):
            connected = int(reason_code) == 0
            with lock:
                state["connected"] = connected
                state["error"] = None if connected else f"Broker recusou a conexão (código {int(reason_code)})."
            if connected:
                client.subscribe(state["config"].get("topic", "#"), qos=0)

        def on_disconnect(_client, _userdata, *_args):
            with lock:
                state["connected"] = False

        def on_message(_client, _userdata, message):
            item = {
                "topic": message.topic,
                "payload": message.payload.decode("utf-8", errors="replace"),
                "retained": bool(message.retain),
                "time": datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S"),
            }
            with lock:
                messages.append(item)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        with lock:
            state["client"] = client
            state["connected"] = False
            state["error"] = "Conectando ao broker…"
        client.connect_async(config["host"], int(config["port"]), keepalive=60)
        client.loop_start()
    except Exception as exc:
        with lock:
            state["connected"] = False
            state["error"] = f"Não foi possível iniciar a conexão ({type(exc).__name__})."


def disconnect_and_clear():
    stop_mqtt()
    with lock:
        state["config"] = {}
        state["error"] = "Configurações removidas."
        messages.clear()
    try:
        SETTINGS_FILE.unlink(missing_ok=True)
    except OSError:
        pass


state["config"] = load_settings()
if state["config"].get("host"):
    connect_mqtt(state["config"])
atexit.register(stop_mqtt)


@app.get("/")
def index():
    return HTML_PAGE


@app.get("/api/settings")
def get_settings():
    with lock:
        return jsonify(safe_settings())


@app.post("/api/settings")
def update_settings():
    data = request.get_json(silent=True) or {}
    old = dict(state["config"])
    host = str(data.get("host", "")).strip()
    try:
        port = int(data.get("port", 8883))
    except (TypeError, ValueError):
        return jsonify(error="A porta precisa ser um número."), 400
    if not 1 <= port <= 65535:
        return jsonify(error="Porta inválida."), 400
    if not host:
        return jsonify(error="Informe o host do broker MQTT."), 400
    anonymous = bool(data.get("anonymous"))
    config = {
        "host": host,
        "port": port,
        "username": "" if anonymous else str(data.get("username", "")).strip() or old.get("username", ""),
        "password": "" if anonymous else str(data.get("password", "")) or old.get("password", ""),
        "topic": str(data.get("topic", "#")).strip() or "#",
        "tls": bool(data.get("tls", True)),
        "telegram_token": "" if data.get("clear_telegram") else str(data.get("telegram_token", "")) or old.get("telegram_token", ""),
        "telegram_chat_id": "" if data.get("clear_telegram") else str(data.get("telegram_chat_id", "")).strip() or old.get("telegram_chat_id", ""),
    }
    try:
        save_settings(config)
    except OSError:
        return jsonify(error="Não foi possível salvar as configurações no diretório de dados."), 500
    with lock:
        state["config"] = config
    connect_mqtt(config)
    return jsonify(status="ok", settings=safe_settings(config))


@app.post("/api/settings/clear")
def clear_settings():
    disconnect_and_clear()
    return jsonify(status="ok")


@app.get("/api/status")
def get_status():
    with lock:
        return jsonify(connected=state["connected"], error=state["error"], settings=safe_settings(), messages=list(reversed(messages)))


@app.post("/api/publish")
def publish():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()
    if not topic:
        return jsonify(error="Informe o tópico."), 400
    with lock:
        client, connected = state["client"], state["connected"]
    if not client or not connected:
        return jsonify(error="Conecte-se ao broker antes de publicar."), 503
    try:
        qos = max(0, min(2, int(data.get("qos", 0))))
        info = client.publish(topic, str(data.get("payload", "")), qos=qos, retain=bool(data.get("retain")))
        info.wait_for_publish(timeout=5)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return jsonify(error="O broker não aceitou a publicação."), 502
        return jsonify(status="ok")
    except Exception as exc:
        return jsonify(error=f"Falha ao publicar ({type(exc).__name__})."), 502


@app.post("/api/retained/delete")
def delete_retained():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()
    if not topic:
        return jsonify(error="Informe o tópico retido."), 400
    with lock:
        client, connected = state["client"], state["connected"]
    if not client or not connected:
        return jsonify(error="Conecte-se ao broker antes de apagar."), 503
    try:
        info = client.publish(topic, payload=b"", qos=1, retain=True)
        info.wait_for_publish(timeout=5)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return jsonify(error="O broker não aceitou a remoção."), 502
        return jsonify(status="ok")
    except Exception as exc:
        return jsonify(error=f"Falha ao remover ({type(exc).__name__})."), 502


@app.post("/api/telegram/test")
def test_telegram():
    with lock:
        token = state["config"].get("telegram_token", "")
        chat_id = state["config"].get("telegram_chat_id", "")
    if not token or not chat_id:
        return jsonify(error="Configure o token e o chat ID do Telegram primeiro."), 400
    body = json.dumps({"chat_id": chat_id, "text": "Teste do MQTT Web."}).encode("utf-8")
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10, context=ssl.create_default_context()) as response:
            if response.status == 200:
                return jsonify(status="ok")
            return jsonify(error="O Telegram não confirmou o envio."), 502
    except (URLError, TimeoutError, OSError):
        return jsonify(error="Não foi possível enviar o teste ao Telegram."), 502


HTML_PAGE = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MQTT Web Simples</title><style>
:root{color-scheme:dark;--bg:#10141c;--panel:#1a2230;--line:#2b374a;--text:#eaf0fa;--muted:#9aa9be;--accent:#55c2a3;--danger:#ef7474}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,-apple-system,sans-serif}main{max-width:1050px;margin:auto;padding:28px 18px}h1{margin:0;font-size:26px}h2{font-size:17px;margin:0 0 16px}.sub{color:var(--muted);margin:6px 0 22px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}.row{display:flex;gap:12px;align-items:center}.row>*{flex:1}.status{padding:10px 12px;border-radius:10px;background:#202a38;margin-bottom:16px}.ok{color:var(--accent)}.bad{color:var(--danger)}label{display:block;color:var(--muted);font-size:13px;margin:11px 0 5px}input,select,textarea{width:100%;padding:10px 11px;border-radius:8px;border:1px solid var(--line);background:#111823;color:var(--text);font:inherit}textarea{min-height:90px;resize:vertical}button{border:0;border-radius:8px;padding:10px 14px;background:var(--accent);color:#07120f;font-weight:700;cursor:pointer;margin-top:12px}button.secondary{background:#344257;color:var(--text)}button.danger{background:#672d37;color:#fff}.check{display:flex;align-items:center;gap:8px;margin-top:13px;color:var(--muted)}.check input{width:auto}.hint{color:var(--muted);font-size:12px}.messages{height:340px;overflow:auto;border-top:1px solid var(--line)}.msg{padding:10px 2px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}.topic{color:var(--accent);font-weight:650}.meta{font-size:12px;color:var(--muted);margin-top:4px}.toolbar{display:flex;justify-content:space-between;align-items:center;margin:14px 0}.notice{color:var(--muted);font-size:13px;margin-top:10px}.span2{grid-column:1 / -1}@media(max-width:720px){.grid{grid-template-columns:1fr}.span2{grid-column:auto}.row{display:block}}
</style></head><body><main>
<header><h1>MQTT Web Simples</h1><p class="sub">Configure a conexão, acompanhe mensagens e publique nos tópicos autorizados.</p></header>
<div class="status" id="status">Verificando conexão…</div><div class="grid">
<section class="card"><h2>Conexão e configurações</h2><label>Host do broker</label><input id="host" placeholder="ex.: broker.exemplo.com">
<div class="row"><div><label>Porta</label><input id="port" type="number" value="8883"></div><div><label>Filtro MQTT</label><input id="topic" value="#" placeholder="casa/#"></div></div>
<label>Usuário MQTT (opcional)</label><input id="username" autocomplete="username"><label>Senha MQTT</label><input id="password" type="password" autocomplete="new-password" placeholder="Em branco mantém a senha salva">
<label class="check"><input id="anonymous" type="checkbox"> Conectar sem usuário e senha</label><label class="check"><input id="tls" type="checkbox" checked> Usar TLS com validação de certificado</label>
<label>Token do bot Telegram (opcional)</label><input id="telegram_token" type="password" autocomplete="new-password" placeholder="Em branco mantém o token salvo"><label>Chat ID do Telegram</label><input id="telegram_chat_id" placeholder="Em branco mantém o chat ID salvo">
<label class="check"><input id="clear_telegram" type="checkbox"> Apagar configuração salva do Telegram</label>
<button onclick="saveSettings()">Salvar e conectar</button> <button class="secondary" onclick="testTelegram()">Testar Telegram</button>
<p class="hint">Senha MQTT e dados do Telegram ficam no arquivo privado de configurações do serviço, fora do GitHub.</p><button class="danger" onclick="clearSettings()">Apagar todas as configurações salvas</button></section>
<section class="card"><h2>Publicar mensagem</h2><label>Tópico</label><input id="pub_topic" placeholder="casa/sala/temperatura"><label>Payload</label><textarea id="payload" placeholder="Mensagem a publicar"></textarea>
<div class="row"><div><label>QoS</label><select id="qos"><option>0</option><option>1</option><option>2</option></select></div><label class="check"><input id="retain" type="checkbox"> Reter no broker</label></div><button onclick="publishMessage()">Publicar</button>
<hr style="border:0;border-top:1px solid var(--line);margin:22px 0"><h2>Apagar mensagem retida</h2><label>Tópico retido</label><input id="delete_topic" placeholder="casa/sala/temperatura"><button class="danger" onclick="deleteRetained()">Apagar mensagem retida</button><p class="hint">A remoção envia um payload vazio com retain=true para o broker.</p></section>
<section class="card span2"><div class="toolbar"><h2 style="margin:0">Mensagens recebidas</h2><button class="secondary" onclick="refresh()">Atualizar</button></div><div class="messages" id="messages"></div></section>
</div><p class="notice" id="notice"></p></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,data){const r=await fetch(url,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});let j={};try{j=await r.json()}catch{}if(!r.ok)throw Error(j.error||`Erro ${r.status}`);return j}
function note(t){document.getElementById('notice').textContent=t;setTimeout(()=>{document.getElementById('notice').textContent=''},5000)}
async function loadSettings(){try{const j=await api('/api/settings');for(const k of ['host','port','username','topic'])if(j[k]!==undefined)document.getElementById(k).value=j[k];document.getElementById('tls').checked=j.tls;document.getElementById('password').placeholder=j.mqtt_password_saved?'Senha salva; em branco mantém':'Senha MQTT';document.getElementById('telegram_token').placeholder=j.telegram_configured?'Token salvo; em branco mantém':'Token do bot';}catch(e){note(e.message)}}
async function saveSettings(){try{const d={};for(const k of ['host','port','username','password','topic','telegram_token','telegram_chat_id'])d[k]=document.getElementById(k).value;d.tls=document.getElementById('tls').checked;d.anonymous=document.getElementById('anonymous').checked;d.clear_telegram=document.getElementById('clear_telegram').checked;await api('/api/settings',d);document.getElementById('password').value='';document.getElementById('telegram_token').value='';document.getElementById('telegram_chat_id').value='';document.getElementById('clear_telegram').checked=false;note('Configurações salvas. Conectando ao broker…');await loadSettings();await refresh()}catch(e){note(e.message)}}
async function clearSettings(){if(!confirm('Apagar as credenciais e configurações salvas?'))return;try{await api('/api/settings/clear',{});['host','username','password','telegram_token','telegram_chat_id'].forEach(k=>document.getElementById(k).value='');note('Configurações apagadas.')}catch(e){note(e.message)}}
async function refresh(){try{const j=await api('/api/status');document.getElementById('status').innerHTML=j.connected?'<span class="ok">● Conectado</span>':'<span class="bad">● Desconectado</span> — '+esc(j.error||'');document.getElementById('messages').innerHTML=j.messages.map(m=>`<div class="msg"><div class="topic">${esc(m.topic)}</div><div>${esc(m.payload)}</div><div class="meta">${esc(m.time)} · ${m.retained?'retida':'normal'}</div></div>`).join('')||'<p class="hint">Nenhuma mensagem recebida ainda.</p>';}catch(e){note(e.message)}}
async function publishMessage(){try{await api('/api/publish',{topic:document.getElementById('pub_topic').value,payload:document.getElementById('payload').value,qos:document.getElementById('qos').value,retain:document.getElementById('retain').checked});note('Mensagem publicada.')}catch(e){note(e.message)}}
async function deleteRetained(){const topic=document.getElementById('delete_topic').value;if(!topic)return note('Informe o tópico.');if(!confirm(`Apagar a mensagem retida de ${topic}?`))return;try{await api('/api/retained/delete',{topic});note('Solicitação de remoção enviada.')}catch(e){note(e.message)}}
async function testTelegram(){try{await api('/api/telegram/test',{});note('Mensagem de teste enviada ao Telegram.')}catch(e){note(e.message)}}
loadSettings();refresh();setInterval(refresh,3000);
</script></body></html>'''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
