FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is used by the container healthcheck; postgresql-client for ops/debugging.
# The rest is WeasyPrint's own runtime -- it renders HTML/CSS to PDF through
# Pango and Cairo rather than a bundled engine, so those have to exist at the
# OS level; fonts-dejavu-core is what app/certificate_render.py's template
# actually asks for by name.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl postgresql-client \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libcairo2 \
        fonts-dejavu-core shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Invoked via `sh` so the image does not depend on the exec bit surviving a
# checkout on Windows. See .gitattributes for the LF line-ending guarantee.
CMD ["sh", "./entrypoint.sh"]
