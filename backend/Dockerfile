FROM python:3.11-slim

WORKDIR /app

ARG INSTALL_LIBREDWG=1
ARG LIBREDWG_REF=0.13.3
RUN if [ "$INSTALL_LIBREDWG" = "1" ]; then \
        set -eux; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            build-essential autoconf automake libtool pkg-config gperf \
            git ca-certificates texinfo; \
        git clone --depth=1 --branch="$LIBREDWG_REF" \
            https://github.com/LibreDWG/libredwg.git /tmp/libredwg \
            || git clone --depth=1 https://github.com/LibreDWG/libredwg.git /tmp/libredwg; \
        cd /tmp/libredwg; \
        sh autogen.sh; \
        export CFLAGS="-O2 -g -Wno-error -Wno-error=alloc-size -Wno-error=stringop-overflow -Wno-error=array-bounds"; \
        ./configure --disable-bindings --disable-shared --disable-werror || \
        ./configure --disable-bindings --disable-werror; \
        make -j"$(nproc)"; \
        make install; \
        ldconfig; \
        cd /; \
        rm -rf /tmp/libredwg; \
        apt-get purge -y --auto-remove \
            build-essential autoconf automake libtool pkg-config gperf \
            git texinfo; \
        rm -rf /var/lib/apt/lists/*; \
        which dwgread; \
        dwgread --version; \
    fi

ARG INSTALL_ODA=0
ARG ODA_DEB_URL=""
RUN if [ "$INSTALL_ODA" = "1" ] && [ -n "$ODA_DEB_URL" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            curl ca-certificates \
            libxcb1 libxkbcommon0 libxkbcommon-x11-0 \
            libqt5core5a libqt5gui5 libqt5widgets5 \
            libegl1 libgl1 libfontconfig1 libdbus-1-3 \
            xvfb && \
        curl -fsSL "$ODA_DEB_URL" -o /tmp/oda.deb && \
        apt-get install -y --no-install-recommends /tmp/oda.deb && \
        rm /tmp/oda.deb && \
        rm -rf /var/lib/apt/lists/* && \
        mv /usr/bin/ODAFileConverter /usr/bin/ODAFileConverter.bin && \
        printf '#!/bin/sh\nexec xvfb-run -a /usr/bin/ODAFileConverter.bin "$@"\n' \
            > /usr/bin/ODAFileConverter && \
        chmod +x /usr/bin/ODAFileConverter ; \
    fi

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
