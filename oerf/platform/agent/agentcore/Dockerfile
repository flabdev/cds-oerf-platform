FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e
WORKDIR /app
COPY app.py .
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
USER nobody
CMD ["python", "app.py"]
