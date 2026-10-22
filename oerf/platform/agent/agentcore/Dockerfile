FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e
WORKDIR /app

# The runtime speaks Bedrock through Strands and reads the two tables through
# boto3. Both are pinned by major version; the image is rebuilt on the
# ``agentcore:enabled`` context.
RUN pip install --no-cache-dir "boto3>=1.34,<2" "strands-agents>=1.0"

# The repo root is the build context. Only the modules the runtime imports are
# copied: the shared evidence policy and analysis, the sanitized X-Ray trace,
# the tenant configuration resolver, the DynamoDB item helpers it reads
# through, and the packaged domain defaults.
COPY oerf/__init__.py /app/oerf/__init__.py
COPY oerf/platform/__init__.py /app/oerf/platform/__init__.py
COPY oerf/platform/evidence/ /app/oerf/platform/evidence/
COPY oerf/platform/observability/ /app/oerf/platform/observability/
COPY oerf/platform/storage/ /app/oerf/platform/storage/
COPY oerf/platform/tenancy/ /app/oerf/platform/tenancy/
COPY oerf/platform/agent/__init__.py /app/oerf/platform/agent/__init__.py
COPY oerf/platform/agent/agentcore/app.py /app/oerf/platform/agent/agentcore/app.py
COPY oerf/platform/agent/agentcore/decision.py /app/oerf/platform/agent/agentcore/decision.py
COPY oerf/platform/agent/agentcore/advisory.py /app/oerf/platform/agent/agentcore/advisory.py
COPY domains/ /app/domains/
COPY sim/ /app/sim/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app
EXPOSE 8080
USER nobody
CMD ["python", "-m", "oerf.platform.agent.agentcore.app"]
