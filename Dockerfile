FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin bridge \
    && install -d -o bridge -g bridge /data

WORKDIR /app
COPY --chown=bridge:bridge bridge /app/bridge
COPY --chown=bridge:bridge grader /app/grader

USER bridge
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "bridge.main"]
