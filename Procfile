# timeout 300: textbook PDF ingest can take 1–3 min. workers 1 on Railway avoids OOM (each worker shares RAM).
web: gunicorn app:app --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:$PORT
