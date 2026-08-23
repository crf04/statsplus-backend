web: gunicorn --workers 4 --threads 2 --timeout 180 --keep-alive 5 --max-requests 1000 --max-requests-jitter 100 --bind 0.0.0.0:${PORT} wsgi:app
projection-collector: python scripts/projection_collection_service.py
