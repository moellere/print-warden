# print-warden with a headless OrcaSlicer 2.4.2 and OpenSCAD. Ubuntu 24.04 because the Orca AppImage needs glibc 2.38.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates xvfb xauth python3 python3-venv openscad libwebkit2gtk-4.1-0 libgtk-3-0t64 libopengl0 \
    libglu1-mesa libegl1 libgstreamer-plugins-base1.0-0 libgstreamer1.0-0 libsecret-1-0 libfuse2t64 tzdata \
  && rm -rf /var/lib/apt/lists/*
RUN curl -L --fail -o /tmp/orca.AppImage \
      https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage \
  && chmod +x /tmp/orca.AppImage && cd /opt && /tmp/orca.AppImage --appimage-extract >/dev/null \
  && mv squashfs-root orcaslicer && rm /tmp/orca.AppImage
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY slicer/house-rules.json /opt/slicer/house-rules.json
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir . && rm -rf /root/.cache
ENV PATH=/venv/bin:$PATH PRINT_WARDEN_CONFIG=/config/printers.yaml
EXPOSE 8710
CMD ["print-warden"]
