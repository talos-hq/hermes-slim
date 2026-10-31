# Local hermes-slim bundle loop (bosn.toml): the same alpine:3.22 the
# linux-x64-musl CI job builds in.
FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f
RUN apk add --no-cache bash python3 git tar curl zstd build-base
RUN git config --global --add safe.directory /repo
