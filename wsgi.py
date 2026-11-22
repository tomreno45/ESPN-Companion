"""Entry point for gunicorn on Railway: `gunicorn wsgi:app`."""
from app import create_app

app = create_app()
