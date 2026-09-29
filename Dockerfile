FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home gills \
    && mkdir -p /var/lib/gills /etc/gills \
    && chown gills:gills /var/lib/gills
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
USER gills
VOLUME ["/var/lib/gills"]
ENTRYPOINT ["gills", "--config", "/etc/gills/config.yml"]
CMD ["check"]
