# Real-Android tests for the trustmux PWA: an x86_64 Android emulator (KVM)
# running real Chrome, driven with adb input (so Chrome's own touch-selection
# handles, long press and gestures are what get exercised) and read back over
# Chrome's remote debugging. Built and run by run.sh; trustmux itself is
# installed from the mounted source tree at run time.
FROM debian:trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openjdk-21-jre-headless curl unzip ca-certificates \
      libpulse0 libnss3 libgl1 libxcomposite1 libxcursor1 libxdamage1 \
      libxi6 libxtst6 libasound2 libdbus-1-3 libxkbfile1 libbsd0 \
      openssl procps python3 python3-pip python3-websocket tmux \
 && rm -rf /var/lib/apt/lists/*

ENV ANDROID_SDK_ROOT=/sdk ANDROID_HOME=/sdk
ENV PATH=/sdk/platform-tools:/sdk/emulator:/sdk/cmdline-tools/latest/bin:$PATH
ARG CMDLINE_TOOLS=commandlinetools-linux-15859902_latest.zip
ARG SYSTEM_IMAGE="system-images;android-37.0;google_apis;x86_64"

RUN mkdir -p /sdk/cmdline-tools \
 && cd /sdk/cmdline-tools \
 && curl -fsSL -o t.zip "https://dl.google.com/android/repository/$CMDLINE_TOOLS" \
 && unzip -q t.zip && mv cmdline-tools latest && rm t.zip \
 && yes | sdkmanager --licenses >/dev/null \
 && sdkmanager "platform-tools" "emulator" "$SYSTEM_IMAGE" >/dev/null

# A phone-shaped device: 1080x2400 at 420dpi (a typical recent Android).
RUN echo no | avdmanager create avd -n phone -k "$SYSTEM_IMAGE" -d pixel_7 >/dev/null \
 && printf '%s\n' 'hw.keyboard=no' 'hw.gpu.enabled=yes' 'hw.ramSize=3072' \
      'disk.dataPartition.size=4G' >> /root/.android/avd/phone.avd/config.ini
