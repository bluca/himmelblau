ARG FEDORA_RELEASE
FROM registry.fedoraproject.org/fedora:${FEDORA_RELEASE}

RUN dnf --assumeyes --setopt=install_weak_deps=False install \
    python3 dpkg cpio rpm dnf openssl tar

WORKDIR /work
