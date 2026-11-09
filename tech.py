"""Технический раздел суперадмина (/tech). Только чтение, сотрудникам не виден."""
import glob
import os
import sys
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

bp = Blueprint("tech", __name__, url_prefix="/tech")
core = None  # сюда app.py передаёт свои общие функции
STARTED_AT = None
SESSION_HOURS = 8


def init_app(app, core_module):
    global core, STARTED_AT
    core = core_module
    STARTED_AT = core.now_local()
    app.register_blueprint(bp)


def enabled():
    """Раздел включён, только если в .env есть логин и хеш пароля (сам пароль не хранится)."""
    login = os.environ.get("SUPERADMIN_LOGIN", "").strip()
    password_hash = os.environ.get("SUPERADMIN_PASSWORD_HASH", "").strip()
    return bool(login) and password_hash.startswith("$argon2")


@bp.before_request
def _check_enabled():
    if not enabled():
        abort(404)


@bp.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _session_valid():
    if not session.get("tech_admin"):
        return False
    try:
        since = datetime.fromisoformat(session.get("tech_since", ""))
    except ValueError:
        return False
    return core.now_local() - since <= timedelta(hours=SESSION_HOURS)


def tech_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not _session_valid():
            if session.get("tech_admin"):
                session.clear()
            return redirect(url_for("tech.login"))
        return func(*args, **kwargs)
    return wrapper


def page(template, **ctx):
    return render_template(template, **ctx)


@bp.route("/vhod", methods=["GET", "POST"])
def login():
    if request.method == "GET" and _session_valid():
        return redirect(url_for("tech.index"))
    error = None
    if request.method == "POST":
        ip = request.remote_addr
        login_text = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        if core.TECH_LIMITER.blocked(ip):
            core.log.warning("ТЕХ-ВХОД заблокирован (много попыток) с %s", ip)
            error = "Слишком много попыток. Подождите 15 минут."
        else:
            ok_login = core.secrets_equal(login_text, os.environ.get("SUPERADMIN_LOGIN", "").strip())
            ok_pass = core.check_hash(os.environ.get("SUPERADMIN_PASSWORD_HASH", "").strip(), password)
            if ok_login and ok_pass:
                core.TECH_LIMITER.reset(ip)
                session.clear()
                session.permanent = False
                session["tech_admin"] = True
                session["tech_since"] = core.now_iso()
                core.log.warning("ТЕХ-ВХОД успешно с %s", ip)
                return redirect(url_for("tech.index"))
            core.TECH_LIMITER.fail(ip)
            core.log.warning("ТЕХ-ВХОД неудачно с %s", ip)
            error = "Неверный логин или пароль."
    return page("tech/login.html", error=error, title="Технический вход")


@bp.route("/vyhod", methods=["POST"])
def logout():
    if session.get("tech_admin"):
        core.log.warning("ТЕХ-ВЫХОД с %s", request.remote_addr)
    session.clear()
    return redirect(url_for("tech.login"))


def _size_text(size):
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size / 1024 / 1024:.1f} МБ"


@bp.route("/")
@tech_required
def index():
    db = core.get_db()
    path = core.db_path()
    db_size = os.path.getsize(path) if os.path.exists(path) else 0
    log_size = sum(os.path.getsize(p) for p in glob.glob(core.LOG_PATH + "*") if os.path.isfile(p))
    system = [
        ("Сайт запущен", STARTED_AT.strftime("%d.%m.%Y %H:%M")),
        ("Python", sys.version.split()[0]),
        ("База данных", "{}, заявок {}, записей журнала {}".format(
            _size_text(db_size),
            db.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM events").fetchone()[0])),
        ("Лог приложения", _size_text(log_size)),
    ]
    return page("tech/index.html", s=core.stats_data(), base_url=url_for("tech.index"),
                system=system, active="stats", title="Сквозная статистика")


@bp.route("/zhurnal")
@tech_required
def journal():
    who = request.args.get("kto", "")
    action = request.args.get("deystvie", "")
    req_text = request.args.get("zayavka", "").strip()
    clauses, params = [], []
    if who in core.USERS:
        clauses.append("e.user_id = ?")
        params.append(who)
    else:
        who = ""
    if action in core.EVENT_LABELS:
        clauses.append("e.action = ?")
        params.append(action)
    else:
        action = ""
    if req_text.isdigit():
        clauses.append("e.request_id = ?")
        params.append(int(req_text))
    else:
        req_text = ""
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = core.get_db().execute(
        "SELECT e.*, r.client_name FROM events e LEFT JOIN requests r ON r.id = e.request_id "
        f"{where} ORDER BY e.at DESC, e.id DESC LIMIT 500", params).fetchall()
    return page("tech/zhurnal.html", rows=rows, who=who, action=action, req_text=req_text,
                active="journal", title="Журнал действий")


@bp.route("/zayavka/<int:request_id>")
@tech_required
def request_detail(request_id):
    db = core.get_db()
    row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if row is None:
        abort(404)
    events = db.execute("SELECT * FROM events WHERE request_id=? ORDER BY at, id", (request_id,)).fetchall()
    return page("tech/zayavka.html", r=row, fields=list(row.keys()), events=events,
                active="journal", title=f"Заявка №{request_id}")


@bp.route("/log")
@tech_required
def log_view():
    mode = request.args.get("chto", "all")
    if mode not in ("all", "vhod", "warn"):
        mode = "all"
    try:
        limit = int(request.args.get("strok", "300"))
    except ValueError:
        limit = 300
    limit = max(50, min(5000, limit))
    needle = request.args.get("soderzhit", "").strip()
    lines = []
    try:
        with open(core.LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    lines.reverse()
    out = []
    for line in lines:
        if mode == "vhod" and "ВХОД" not in line and "ВЫХОД" not in line:
            continue
        if mode == "warn" and " WARNING " not in line and " ERROR " not in line:
            continue
        if needle and needle.casefold() not in line.casefold():
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return page("tech/log.html", lines=out, mode=mode, limit=limit, needle=needle,
                active="log", title="Лог приложения")
