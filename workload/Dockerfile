# syntax=docker/dockerfile:1.7
# One image, three roles. SERVICE_NAME and DOWNSTREAM select behaviour, so the
# reference topology is configuration rather than code.
#
# The build args make a *version* of the image. Two tags come from this one
# file (`make workload-images`): a healthy one, and one whose BAKED_FAULT is
# listed for its own version, so the fault ships with the deploy the way a real
# regression does. See service.py, "Faults that ship in the image".
FROM python:3.14-slim-bookworm

ARG WORKLOAD_VERSION=1.0.0
ARG BAKED_FAULT=""
ARG DEPENDENCY_NAME=""
ARG DEPENDENCY_VERSION=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKLOAD_VERSION=${WORKLOAD_VERSION} \
    BAKED_FAULT=${BAKED_FAULT} \
    BAKED_FAULT_VERSIONS=${WORKLOAD_VERSION} \
    DEPENDENCY_NAME=${DEPENDENCY_NAME} \
    DEPENDENCY_VERSION=${DEPENDENCY_VERSION}

# Read by the runtime adapter and by operators through `docker inspect`, so the
# version and the dependency it carries are facts about the image, not claims
# made by the process running in it.
LABEL org.opencontainers.image.title="aegis-workload" \
      org.opencontainers.image.version="${WORKLOAD_VERSION}" \
      aegis.version="${WORKLOAD_VERSION}" \
      aegis.dependency="${DEPENDENCY_NAME}" \
      aegis.dependency.version="${DEPENDENCY_VERSION}"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# The dependency the labels name is the one actually installed, so the label is
# a fact about the image rather than a story told about it.
RUN if [ -n "${DEPENDENCY_NAME}" ] && [ -n "${DEPENDENCY_VERSION}" ]; then \
      pip install --no-cache-dir "${DEPENDENCY_NAME}==${DEPENDENCY_VERSION}"; \
    fi

COPY service.py ./

# The image's identity as a file, read by service.py in preference to ENV: a
# container recreated on this tag by copying another container's environment
# would otherwise report - and behave as - the version it replaced.
RUN printf '{"version":"%s","baked_fault":"%s","baked_fault_versions":"%s","dependency":"%s","dependency_version":"%s"}\n' \
      "${WORKLOAD_VERSION}" "${BAKED_FAULT}" "${WORKLOAD_VERSION}" \
      "${DEPENDENCY_NAME}" "${DEPENDENCY_VERSION}" > /app/build-info.json

RUN groupadd --system --gid 10002 wl \
 && useradd --system --uid 10002 --gid wl wl
USER wl

EXPOSE 8080
# Lets a redeploy wait for the replacement to serve before it joins the
# service's DNS name, so a rollback does not route traffic to a booting process.
HEALTHCHECK --interval=2s --timeout=2s --start-period=2s --retries=15   CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
