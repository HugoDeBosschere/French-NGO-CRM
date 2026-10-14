# Copie de référence du Dockerfile en service sur le serveur
# (/opt/volunteer-apps/apps/website-meeting/Dockerfile), versionnée parce
# qu'elle ne l'était pas. Voir deploy/README.md.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY app.py ./
COPY templates/ ./templates/
COPY static/ ./static/
# utils/ et actual_dataset/ voyagent avec app.py, et c'est le point important :
# les scripts lisent POLITICAL_GROUPS et POLITICAL_ROLES dans app.py, donc deux
# provenances différentes les font diverger. Le 12/10/2026, l'image portait un
# app.py de la veille et utils/ venait d'un checkout à jour : le garde-fou a
# refusé de tourner pour une incohérence qui n'existait que dans l'assemblage.
COPY utils/ ./utils/
COPY actual_dataset/ ./actual_dataset/

# Install dependencies from pyproject.toml using pip
RUN pip install --no-cache-dir flask gunicorn python-magic

RUN mkdir -p /app/data /app/uploads

EXPOSE 5000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
