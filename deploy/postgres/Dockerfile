FROM postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5

# A regular image file avoids host bind-mount executable-bit ambiguity.
# The official entrypoint sources non-executable shell initialization files.
COPY --chmod=0444 init-roles.sh /docker-entrypoint-initdb.d/010-roles.sh
