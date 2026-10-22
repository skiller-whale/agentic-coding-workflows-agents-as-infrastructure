FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && git config --system --add safe.directory /workspace/ticket_parser \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

FROM base AS whalehub

COPY whalehub/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY whalehub/app.py ./app.py
COPY whalehub/templates ./templates
COPY whalehub/static ./static
COPY whalehub/data ./data

EXPOSE 8000
CMD ["python", "app.py"]

FROM base AS runner

ENV PATH="/root/.local/bin:${PATH}" \
    DISABLE_AUTOUPDATER=1

# The native installer supports an exact release and needs no Node runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl --fail --silent --show-error --location --max-time 60 https://claude.ai/install.sh -o /tmp/install-claude.sh \
    && bash /tmp/install-claude.sh 2.1.293 \
    && test "$(claude --version)" = "2.1.293 (Claude Code)" \
    && claude --max-turns 12 --help > /dev/null \
    && rm /tmp/install-claude.sh \
    && rm -rf /var/lib/apt/lists/*

COPY ticket_parser/requirements.txt ./requirements.txt
COPY runner/requirements.txt ./runner-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r runner-requirements.txt
COPY runner/worker.py ./worker.py
COPY runner/issue_tools.py ./issue_tools.py

WORKDIR /workspace/ticket_parser
CMD ["python", "/app/worker.py"]
