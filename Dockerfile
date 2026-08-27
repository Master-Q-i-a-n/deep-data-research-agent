# Keep the production server aligned with the installed LangGraph API and
# the Python version required by pyproject.toml.
ARG LANGGRAPH_API_VERSION=0.11.1
FROM langchain/langgraph-api:${LANGGRAPH_API_VERSION}-py3.13

WORKDIR /deps/deep-data-research-agent

# Install the locked production dependency set before copying application code,
# so source-only changes can reuse the dependency layer in GitHub Actions.
COPY pyproject.toml uv.lock README.md ./
RUN uv export \
        --frozen \
        --no-dev \
        --no-emit-project \
        --no-header \
        --no-annotate \
        --format requirements-txt \
        --output-file /tmp/requirements.txt \
    && PYTHONDONTWRITEBYTECODE=1 uv pip install \
        --system \
        --no-cache-dir \
        -c /api/constraints.txt \
        -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# .dockerignore keeps credentials and local state out of these copies. Public
# Skill seeds stay under src and remain available to the current startup sync.
COPY src ./src
COPY scripts/maintenance ./scripts/maintenance
COPY alembic.ini langgraph.json ./

# Install only the project after its locked dependencies are already present.
RUN PYTHONDONTWRITEBYTECODE=1 uv pip install \
    --system \
    --no-cache-dir \
    -c /api/constraints.txt \
    --no-deps \
    -e /deps/deep-data-research-agent

# Use absolute POSIX paths because this image is built on a Linux runner even
# when the source repository is maintained on Windows.
ENV LANGGRAPH_STORE='{"path":"/deps/deep-data-research-agent/src/deep_data_research_agent/infrastructure/mongodb/store.py:create_mongodb_store"}' \
    LANGGRAPH_AUTH='{"path":"/deps/deep-data-research-agent/src/deep_data_research_agent/api/auth.py:auth"}' \
    LANGGRAPH_HTTP='{"app":"/deps/deep-data-research-agent/src/deep_data_research_agent/api/app.py:app","enable_custom_route_auth":true,"middleware_order":"auth_first","cors":{"allow_origins":["http://127.0.0.1:5174","http://localhost:5174"],"allow_methods":["*"],"allow_headers":["Authorization","Content-Type"],"allow_credentials":false}}' \
    LANGGRAPH_CHECKPOINTER='{"path":"/deps/deep-data-research-agent/src/deep_data_research_agent/infrastructure/postgres/checkpointer.py:create_user_checkpointer"}' \
    LANGSERVE_GRAPHS='{"supervisor":"/deps/deep-data-research-agent/src/deep_data_research_agent/agents/supervisor.py:graph","crawl-worker":"/deps/deep-data-research-agent/src/deep_data_research_agent/agents/crawl_worker.py:graph"}'

# Restore the base server package if a user dependency attempted to replace it,
# then remove build tooling from the runtime image.
RUN mkdir -p /api/langgraph_api /api/langgraph_runtime /api/langgraph_license \
    && touch /api/langgraph_api/__init__.py \
        /api/langgraph_runtime/__init__.py \
        /api/langgraph_license/__init__.py \
    && PYTHONDONTWRITEBYTECODE=1 uv pip install \
        --system \
        --no-cache-dir \
        --no-deps \
        -e /api \
    && pip uninstall -y pip setuptools wheel \
    && rm -rf /usr/local/lib/python*/site-packages/pip* \
        /usr/local/lib/python*/site-packages/setuptools* \
        /usr/local/lib/python*/site-packages/wheel* \
        /usr/lib/python*/site-packages/pip* \
        /usr/lib/python*/site-packages/setuptools* \
        /usr/lib/python*/site-packages/wheel* \
    && find /usr/local/bin /usr/bin -name 'pip*' -delete \
    && uv pip uninstall --system pip setuptools wheel \
    && rm -f /usr/bin/uv /usr/bin/uvx

EXPOSE 8000
