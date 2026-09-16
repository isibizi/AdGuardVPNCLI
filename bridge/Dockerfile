# AdGuard VPN -> WireGuard Bridge
#
# Build context is the REPOSITORY ROOT, because the official AdGuard installer
# (scripts/release/install.sh, kept in sync with upstream by CI) is copied in
# and executed during the build:
#
#   docker build -f bridge/Dockerfile -t adguard-wg-bridge ..
#
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH

# Pin the tun2socks release. Checksums are optional but strongly recommended:
# download the asset once, run `sha256sum` on it and pass the value as a build
# arg (see bridge/README.md -> "tun2socks aktualisieren").
ARG TUN2SOCKS_VERSION=v2.6.0
ARG TUN2SOCKS_SHA256_AMD64=""
ARG TUN2SOCKS_SHA256_ARM64=""

# hexdump (bsdextrautils) and gawk are required by the AdGuard install script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bsdextrautils \
        ca-certificates \
        conntrack \
        curl \
        gawk \
        iproute2 \
        iptables \
        iputils-ping \
        procps \
        python3 \
        python3-pip \
        python3-venv \
        sed \
        supervisor \
        tar \
        unzip \
        wireguard-tools \
    && rm -rf /var/lib/apt/lists/*

# Userspace WireGuard, used automatically when the host has no kernel module
# (LXC/OpenVZ VPS). Not available in every Debian suite, so a missing package
# must not fail the build - the entrypoint reports it if the fallback is needed
# but absent.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends wireguard-go \
        || echo "WARN: wireguard-go unavailable; kernel WireGuard will be required") \
    && rm -rf /var/lib/apt/lists/*

# --- AdGuard VPN CLI -------------------------------------------------------
# `-a y` is mandatory: without it the script reads from /dev/tty and the build
# would hang waiting for an answer that can never arrive.
COPY scripts/release/install.sh /tmp/adguard-install.sh
RUN sh /tmp/adguard-install.sh -o /opt/adguardvpn_cli -a y -v \
    && rm -f /tmp/adguard-install.sh \
    && /opt/adguardvpn_cli/adguardvpn-cli --version

# --- tun2socks -------------------------------------------------------------
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) t2s_arch=amd64; t2s_sum="${TUN2SOCKS_SHA256_AMD64}" ;; \
      arm64) t2s_arch=arm64; t2s_sum="${TUN2SOCKS_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/xjasonlyu/tun2socks/releases/download/${TUN2SOCKS_VERSION}/tun2socks-linux-${t2s_arch}.zip"; \
    curl -fsSL -o /tmp/tun2socks.zip "$url"; \
    if [ -n "$t2s_sum" ]; then \
      echo "${t2s_sum}  /tmp/tun2socks.zip" | sha256sum -c -; \
    else \
      echo "WARN: no SHA256 pinned for tun2socks (${t2s_arch}); see bridge/README.md" >&2; \
    fi; \
    unzip -p /tmp/tun2socks.zip "tun2socks-linux-${t2s_arch}" > /usr/local/bin/tun2socks; \
    chmod +x /usr/local/bin/tun2socks; \
    rm -f /tmp/tun2socks.zip; \
    /usr/local/bin/tun2socks --version

# --- control panel ---------------------------------------------------------
COPY bridge/requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/panel-venv \
    && /opt/panel-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/panel-venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY bridge/rootfs/ /
COPY bridge/app/ /opt/panel/app/
RUN chmod +x /usr/local/bin/bridge-*.sh

ENV PATH="/opt/adguardvpn_cli:/opt/panel-venv/bin:${PATH}" \
    XDG_DATA_HOME=/data/adguard \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/panel \
    BRIDGE_DATA_DIR=/data

EXPOSE 51820/udp 8080/tcp
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD /usr/local/bin/bridge-net.sh status

ENTRYPOINT ["/usr/local/bin/bridge-entrypoint.sh"]
