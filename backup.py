"""Резервные копии базы и фото: создание, проверка, восстановление.

Сайт сам делает копию раз в день (см. start_backup_thread в app.py). Вручную:
    python backup.py                       — сделать копию сейчас
    python backup.py --proverit ФАЙЛ       — проверить копию
    python backup.py --vosstanovit ФАЙЛ    — восстановить (сайт на это время остановить)
"""
import os
import shutil
import sqlite3
import sys
import zipfile
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEEP_DAYS = 14


def backup_dir():
    return os.environ.get("BACKUP_DIR") or os.path.join(BASE_DIR, "data", "backups")


def uploads_dir():
    return os.environ.get("UPLOADS_DIR") or os.path.join(BASE_DIR, "data", "uploads")


def verify(path):
    """Проверить файл базы. Вернуть (всё ли хорошо, описание)."""
    if not os.path.exists(path):
        return False, "файла нет"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "база повреждена"
            requests = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"не открывается: {exc}"
    return True, f"заявок {requests}, записей журнала {events}"


def _prune(directory, suffix):
    names = sorted(n for n in os.listdir(directory) if n.startswith("zayavki-") and n.endswith(suffix)
                   and (suffix != ".db" or not n.endswith("-photos.zip")))
    for name in names[:-KEEP_DAYS]:
        os.remove(os.path.join(directory, name))


def make_backup(db_path, today=None, directory=None):
    """Сделать копию базы (и архив фото, если есть). Вернуть путь к копии базы."""
    today = today or date.today()
    directory = directory or backup_dir()
    os.makedirs(directory, exist_ok=True)
    final = os.path.join(directory, f"zayavki-{today.isoformat()}.db")
    tmp = final + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)  # согласованная копия, даже пока сайт работает
    finally:
        dst.close()
        src.close()
    ok, info = verify(tmp)
    if not ok:
        os.remove(tmp)
        raise RuntimeError("Копия не прошла проверку: " + info)
    os.replace(tmp, final)

    photos = uploads_dir()
    if os.path.isdir(photos) and any(files for _r, _d, files in os.walk(photos)):
        archive = os.path.join(directory, f"zayavki-{today.isoformat()}-photos.zip")
        with zipfile.ZipFile(archive + ".tmp", "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(photos):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, photos))
        os.replace(archive + ".tmp", archive)
    _prune(directory, ".db")
    _prune(directory, "-photos.zip")
    return final


def run_daily(db_path, today=None, directory=None):
    """Сделать копию за сегодня, если её ещё нет. Вернуть путь или None."""
    today = today or date.today()
    directory = directory or backup_dir()
    if not os.path.exists(db_path):
        return None
    if os.path.exists(os.path.join(directory, f"zayavki-{today.isoformat()}.db")):
        return None
    return make_backup(db_path, today, directory)


def restore(backup_path, db_path, photos_zip=None):
    """Восстановить базу (и фото) из копии. Текущая база сначала сохраняется рядом."""
    ok, info = verify(backup_path)
    if not ok:
        raise RuntimeError("Копия плохая, восстанавливать нельзя: " + info)
    if photos_zip:
        target = os.path.abspath(uploads_dir())
        with zipfile.ZipFile(photos_zip) as z:
            for member in z.namelist():
                full = os.path.abspath(os.path.join(target, member))
                if os.path.commonpath([target, full]) != target:
                    raise RuntimeError("В архиве фото есть пути за пределы папки — архив отклонён.")
    if os.path.exists(db_path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db_path, f"{db_path}.before-restore-{stamp}")
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    shutil.copy2(backup_path, db_path)
    if photos_zip:
        os.makedirs(uploads_dir(), exist_ok=True)
        with zipfile.ZipFile(photos_zip) as z:
            z.extractall(uploads_dir())
    return verify(db_path)


def _default_db():
    return os.environ.get("DATABASE_PATH") or os.path.join(BASE_DIR, "data", "zayavki.db")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--proverit"] and len(args) == 2:
        print(*verify(args[1]))
    elif args[:1] == ["--vosstanovit"] and len(args) in (2, 3):
        print("Сайт на время восстановления должен быть остановлен.")
        print("Результат:", *restore(args[1], _default_db(), args[2] if len(args) == 3 else None))
    else:
        print("Копия создана:", make_backup(_default_db()))
