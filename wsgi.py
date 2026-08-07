"""Production WSGI entry point.

Run with: waitress-serve --listen=0.0.0.0:8080 wsgi:app
Put this behind an HTTPS reverse proxy in production.
"""
from app import app

