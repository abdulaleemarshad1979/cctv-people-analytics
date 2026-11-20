"""Gunicorn entry point: one worker (cameras and models live in this process), many threads."""
from app import app, create_engine

create_engine()
