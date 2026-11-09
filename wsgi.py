"""Точка входа для боевого сервера на хостинге: gunicorn wsgi:app (режим отладки здесь не включается)."""
from app import app  # noqa: F401
