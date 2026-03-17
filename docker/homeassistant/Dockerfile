FROM homeassistant/home-assistant:stable

WORKDIR /opt/devi

COPY README.md /opt/devi/README.md
COPY pyproject.toml /opt/devi/pyproject.toml
COPY pydevi /opt/devi/pydevi

RUN pip install --no-cache-dir /opt/devi
