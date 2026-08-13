FROM alpine:3.22 AS guest-root

RUN apk add --no-cache linux-virt python3 util-linux
RUN mkdir -p /opt/menti /guest-assets /input /work
COPY grader/guest/menti-init /sbin/menti-init
COPY grader/guest_case_runner.py /opt/menti/guest_case_runner.py
COPY grader/guest_supervisor.py /opt/menti/guest_supervisor.py
RUN chmod 0755 /sbin/menti-init \
    && chmod 0555 /opt/menti/guest_case_runner.py /opt/menti/guest_supervisor.py \
    && cp /boot/vmlinuz-virt /guest-assets/vmlinuz \
    && cp /boot/initramfs-virt /guest-assets/initramfs

FROM debian:13-slim AS guest-image

RUN apt-get update \
    && apt-get install --yes --no-install-recommends e2fsprogs \
    && rm -rf /var/lib/apt/lists/*
COPY --from=guest-root / /guest-root
RUN mkdir -p /assets \
    && cp /guest-root/guest-assets/vmlinuz /assets/vmlinuz \
    && cp /guest-root/guest-assets/initramfs /assets/initramfs \
    && truncate -s 192M /assets/rootfs.ext4 \
    && mke2fs -q -t ext4 -d /guest-root -L MENTI_ROOT /assets/rootfs.ext4 \
    && chmod 0444 /assets/rootfs.ext4 /assets/vmlinuz /assets/initramfs

FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends e2fsprogs qemu-system-x86 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin launcher \
    && install -d -o launcher -g launcher -m 0700 /run/menti-launcher

WORKDIR /app
COPY --chown=launcher:launcher grader /app/grader
COPY --from=guest-image /assets /opt/menti-vm

USER launcher

ENTRYPOINT ["python", "-m", "grader.qemu_launcher_server"]
CMD ["--socket", "/run/menti-launcher/launcher.sock", "--assets", "/opt/menti-vm", "--allow-uid", "1000"]
