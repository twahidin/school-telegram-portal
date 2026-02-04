# timeout 300: textbook PDF ingest can take 1–3 min.
# workers 1, threads 2: Minimize memory on Railway free tier.
web: gunicorn app:app --workers 1 --threads 2 --timeout 300 --bind 0.0.0.0:$PORT
