# Sandbox image for arxiv2epub (see sandbox.py). Built automatically on first use.
FROM debian:trixie-slim

# Everything texlive-full pulls in except its multi-GB documentation, plus
# LaTeXML, Ghostscript (EPS figures), a serif font for the cover, and Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      $(apt-cache depends texlive-full | awk '/Depends:/ {print $2}' | grep -v -- '-doc$') \
      latexml ghostscript fonts-dejavu-core python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

ARG DEPS
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir $DEPS

COPY . /opt/app/arxiv2epub
RUN /opt/venv/bin/python -m compileall -q /opt/app

# The root filesystem is read-only at run time; HOME points at the /tmp tmpfs.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["python", "-m", "arxiv2epub.cli"]
