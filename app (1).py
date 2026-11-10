"""Thoth Education - servidor para Databricks Apps.

Serve o index.html e expõe a API de armazenamento usada pelo sistema:
  GET    /api/health
  GET    /api/kv/<colecao>/<id>
  PUT    /api/kv/<colecao>/<id>
  DELETE /api/kv/<colecao>/<id>
  POST   /api/send-mail        (e-mail de acesso liberado e de redefinição de senha, via SMTP)

Os dados (bases auxiliares) ficam em um Volume do Unity Catalog, em
<THOTH_VOLUME_PATH>/kv/<colecao>/<id>.json. Para desenvolvimento local, defina
THOTH_LOCAL_DIR para gravar em uma pasta do computador.
"""
import base64
import hashlib
import hmac
import html as htmllib
import io
import json
import logging
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage

from flask import Flask, Response, jsonify, request, send_from_directory

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VOLUME_PATH = os.environ.get("THOTH_VOLUME_PATH", "").rstrip("/")
LOCAL_DIR = os.environ.get("THOTH_LOCAL_DIR", "").rstrip("/")
SAFE = re.compile(r"^[A-Za-z0-9_.\-]{1,200}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# E-mail (SMTP). SMTP_PASSWORD deve vir de um secret do Databricks (app.yaml: valueFrom).
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "true").strip().lower() not in ("0", "false", "no")
APP_URL = os.environ.get("THOTH_APP_URL", "").strip().rstrip("/")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # limite por requisição (o app envia partes de ~3 MB)
log = logging.getLogger("thoth")


class LocalStore:
    kind = "local"

    def __init__(self, base):
        self.base = base

    def _path(self, c, i):
        return os.path.join(self.base, "kv", c, i + ".json")

    def check(self):
        os.makedirs(self.base, exist_ok=True)

    def get(self, c, i):
        try:
            with open(self._path(c, i), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def put(self, c, i, data):
        p = self._path(c, i)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def delete(self, c, i):
        try:
            os.remove(self._path(c, i))
        except FileNotFoundError:
            pass


class VolumeStore:
    kind = "volume"

    def __init__(self, base):
        from databricks.sdk import WorkspaceClient  # autenticação automática do Databricks App

        self.w = WorkspaceClient()
        self.base = base
        self._dirs = set()

    def _path(self, c, i):
        return f"{self.base}/kv/{c}/{i}.json"

    def check(self):
        self.w.files.get_directory_metadata(self.base)

    def get(self, c, i):
        from databricks.sdk.errors import NotFound

        try:
            return self.w.files.download(self._path(c, i)).contents.read()
        except NotFound:
            return None

    def put(self, c, i, data):
        d = f"{self.base}/kv/{c}"
        if d not in self._dirs:
            self.w.files.create_directory(d)  # idempotente
            self._dirs.add(d)
        self.w.files.upload(file_path=self._path(c, i), contents=io.BytesIO(data), overwrite=True)

    def delete(self, c, i):
        from databricks.sdk.errors import NotFound

        try:
            self.w.files.delete(self._path(c, i))
        except NotFound:
            pass


def make_store():
    if LOCAL_DIR:
        return LocalStore(LOCAL_DIR)
    if VOLUME_PATH:
        return VolumeStore(VOLUME_PATH)
    return None


STORE = None
STORE_ERR = None
try:
    STORE = make_store()
except Exception as e:  # noqa: BLE001
    STORE_ERR = str(e)
    log.exception("Falha ao iniciar o armazenamento")


def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


def err(status, msg):
    return no_cache(jsonify({"error": msg})), status


def valid(c, i):
    return bool(SAFE.match(c)) and bool(SAFE.match(i))


def mail_ready():
    return bool(SMTP_HOST and SMTP_FROM)


def smtp_send(to, subject, html_body, text_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    with server as s:
        if SMTP_PORT != 465 and SMTP_STARTTLS:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


def nome_do_email(email):
    parts = [p for p in re.split(r"[._-]+", email.split("@")[0]) if p]
    return " ".join(p[:1].upper() + p[1:].lower() for p in parts)


def read_cfg(doc_id):
    raw = STORE.get("config", doc_id)
    if not raw:
        return None
    try:
        return json.loads(raw).get("v")
    except Exception:  # noqa: BLE001
        return None


def password_matches(user, password):
    pw = (user or {}).get("pw") or {}
    try:
        salt = base64.b64decode(pw["s"])
        want = base64.b64decode(pw["h"])
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(pw.get("i") or 210000), len(want))
        return hmac.compare_digest(got, want)
    except Exception:  # noqa: BLE001
        return False


def access_mail(email, password, perfil, link):
    e = htmllib.escape
    nome = nome_do_email(email)
    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;padding:28px;border:1px solid #e2e8f0;border-radius:12px;color:#0f172a">'
        '<h2 style="color:#002f87;margin:0 0 12px">Thoth Education</h2>'
        f"<p>Olá, <b>{e(nome)}</b>! Seu acesso ao Thoth Education foi liberado.</p>"
        '<table style="background:#f1f5f9;border-radius:8px;padding:14px 18px;margin:14px 0;font-size:14px">'
        f'<tr><td style="color:#64748b;padding:3px 14px 3px 0">Login</td><td><b>{e(email)}</b></td></tr>'
        f'<tr><td style="color:#64748b;padding:3px 14px 3px 0">Senha</td><td><b>{e(password)}</b></td></tr>'
        f'<tr><td style="color:#64748b;padding:3px 14px 3px 0">Perfil</td><td><b>{e(perfil)}</b></td></tr></table>'
        f'<p style="margin:22px 0"><a href="{e(link)}" style="background:#2f73ff;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:bold">Acessar o Thoth Education</a></p>'
        '<p style="margin:0 0 6px"><b>Orientações para o primeiro acesso</b></p>'
        '<ol style="font-size:14px;line-height:1.6;padding-left:20px;margin:0 0 14px">'
        f"<li>Clique no botão acima (ou abra {e(link)}). Se o navegador pedir login corporativo, use o seu usuário da empresa.</li>"
        "<li>Na tela de login, informe o login e a senha deste e-mail.</li>"
        '<li>Recomendamos trocar a senha no primeiro acesso: na tela de login clique em <b>"Esqueceu sua senha?"</b>, informe seu e-mail e siga o link recebido (válido por 1 hora) para criar uma nova senha.</li>'
        "<li>Não compartilhe sua senha. Em caso de dúvida, procure um Administrador do sistema.</li></ol>"
        f'<p style="font-size:12px;color:#94a3b8;word-break:break-all">{e(link)}</p></div>'
    )
    text_body = (
        f"Olá, {nome}!\n\nSeu acesso ao Thoth Education foi liberado.\n\n"
        f"Login: {email}\nSenha: {password}\nPerfil: {perfil}\n\nAcesse: {link}\n\n"
        "Primeiro acesso:\n"
        "1. Abra o link acima. Se o navegador pedir login corporativo, use o seu usuário da empresa.\n"
        "2. Na tela de login, informe o login e a senha deste e-mail.\n"
        '3. Recomendamos trocar a senha: na tela de login clique em "Esqueceu sua senha?", informe seu e-mail e siga o link recebido (válido por 1 hora).\n'
        "4. Não compartilhe sua senha. Em caso de dúvida, procure um Administrador.\n"
    )
    return "Thoth Education - Acesso liberado", html_body, text_body


def reset_mail(link):
    e = htmllib.escape
    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;padding:28px;border:1px solid #e2e8f0;border-radius:12px;color:#0f172a">'
        '<h2 style="color:#002f87;margin:0 0 12px">Thoth Education</h2>'
        "<p>Recebemos uma solicitação para redefinir a senha do seu acesso.</p>"
        f'<p style="margin:24px 0"><a href="{e(link)}" style="background:#2f73ff;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:bold">Criar nova senha</a></p>'
        '<p style="font-size:13px;color:#64748b">O link é válido por 1 hora. Se você não fez esta solicitação, ignore este e-mail.</p>'
        f'<p style="font-size:12px;color:#94a3b8;word-break:break-all">{e(link)}</p></div>'
    )
    text_body = f"Recebemos uma solicitação para redefinir a senha do seu acesso.\nCrie a nova senha (link válido por 1 hora): {link}\nSe não foi você, ignore este e-mail.\n"
    return "Thoth Education | Redefinição de senha", html_body, text_body


@app.get("/")
def index():
    return no_cache(send_from_directory(APP_DIR, "index.html"))


@app.get("/api/health")
def health():
    if STORE is None:
        msg = STORE_ERR or "THOTH_VOLUME_PATH não configurado"
        return no_cache(jsonify({"ok": False, "error": msg}))
    try:
        STORE.check()
        return no_cache(jsonify({"ok": True, "storage": STORE.kind, "mail": mail_ready(), "ts": int(time.time())}))
    except Exception as e:  # noqa: BLE001
        log.exception("Healthcheck falhou")
        return no_cache(jsonify({"ok": False, "error": str(e)}))


@app.get("/api/kv/<c>/<i>")
def kv_get(c, i):
    if STORE is None or not valid(c, i):
        return err(400, "requisição inválida")
    try:
        data = STORE.get(c, i)
    except Exception as e:  # noqa: BLE001
        log.exception("GET falhou")
        return err(500, str(e))
    if data is None:
        return err(404, "não encontrado")
    return no_cache(Response(data, mimetype="application/json"))


@app.put("/api/kv/<c>/<i>")
def kv_put(c, i):
    if STORE is None or not valid(c, i):
        return err(400, "requisição inválida")
    data = request.get_data()
    if not data:
        return err(400, "corpo vazio")
    try:
        STORE.put(c, i, data)
    except Exception as e:  # noqa: BLE001
        log.exception("PUT falhou")
        return err(500, str(e))
    return no_cache(jsonify({"ok": True}))


@app.delete("/api/kv/<c>/<i>")
def kv_delete(c, i):
    if STORE is None or not valid(c, i):
        return err(400, "requisição inválida")
    try:
        STORE.delete(c, i)
    except Exception as e:  # noqa: BLE001
        log.exception("DELETE falhou")
        return err(500, str(e))
    return no_cache(jsonify({"ok": True}))


@app.post("/api/send-mail")
def send_mail_route():
    """Envia o e-mail de acesso liberado ou de redefinição. Só envia para usuários cadastrados no sistema."""
    if STORE is None:
        return err(400, "armazenamento não configurado")
    if not mail_ready():
        return err(503, "NOT_CONFIGURED")
    d = request.get_json(silent=True) or {}
    kind = d.get("type")
    to = str(d.get("to", "")).strip().lower()
    link = str(d.get("link", "")).strip()
    if kind not in ("access", "reset") or not EMAIL_RE.match(to) or not re.match(r"^https?://", link):
        return err(400, "requisição inválida")
    try:
        users = read_cfg("USERS") or []
        user = next((u for u in users if str(u.get("email", "")).lower() == to), None)
        if not user or user.get("blocked"):
            return err(403, "destinatário não cadastrado")
        base = (APP_URL or link.split("#")[0].split("?")[0]).rstrip("/") + "/"
        if kind == "access":
            password = str(d.get("password", ""))
            if not password or not password_matches(user, password):
                return err(403, "senha informada não confere com o cadastro")
            subject, html_body, text_body = access_mail(to, password, str(d.get("profile", ""))[:60], base)
        else:
            m = re.search(r"[?&]reset=([A-Za-z0-9]+)", link)
            tokens = read_cfg("RESETS") or {}
            ent = tokens.get(hashlib.sha256(m.group(1).encode()).hexdigest()) if m else None
            if not ent or str(ent.get("email", "")).lower() != to or (ent.get("exp") or 0) < time.time() * 1000:
                return err(403, "link de redefinição inválido ou expirado")
            subject, html_body, text_body = reset_mail(base + "?reset=" + m.group(1))
        smtp_send(to, subject, html_body, text_body)
    except smtplib.SMTPAuthenticationError:
        log.exception("SMTP: autenticação")
        return err(502, "falha de autenticação no servidor de e-mail (verifique SMTP_USER e SMTP_PASSWORD)")
    except Exception as e:  # noqa: BLE001
        log.exception("Envio de e-mail falhou")
        return err(502, "falha ao enviar o e-mail: " + str(e))
    return no_cache(jsonify({"ok": True}))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")))
