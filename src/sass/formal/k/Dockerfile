# Build with the verified v7.1.337 Ubuntu Noble package as k.deb in the context.
FROM ubuntu:24.04
COPY k.deb /tmp/k.deb
RUN echo '3692997e6bfa4be490d24bd9201289d4a05872889c9da725dda21e99d03d6990  /tmp/k.deb' | sha256sum -c - \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends /tmp/k.deb z3 python3 ca-certificates \
    && rm /tmp/k.deb \
    && rm -rf /var/lib/apt/lists/*
ENV JAVA_TOOL_OPTIONS="-Xmx2g"
WORKDIR /work
