#!/usr/bin/env bash
# The end-to-end run (tests/system/README.md): build the packages of the last stable version and of this state,
# start a Debian 13 container with systemd, play the run inside as root, collect the logs.
#
#   bash tests/system/run.sh [OUT_DIR]
#
# Needs Linux with Docker, git with the tags of the repository, and Python 3.13 or newer with this code installed
# (`pip install .`) for the package build. In the container github.com and api.github.com point to the stand-in
# (--add-host); apt and PyPI are reached as on any installation. Exit code: that of the run.
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/../.." && pwd)"
out="${1:-$(mktemp -d)}" # a folder of its own: the container writes its logs there as root
name="reprintbuddy-e2e-$$"
image="reprintbuddy-e2e:local"

if [ -e "${out}/build" ] || [ -e "${out}/logs" ]; then
    echo "${out} holds an earlier run; give an empty or new folder." >&2
    exit 2
fi
mkdir -p "${out}/build" "${out}/logs"
echo "packages and logs: ${out}"

python3 "${repo}/tests/system/packages.py" --repo "${repo}" --out "${out}/build"

docker build --pull -t "${image}" -f "${here}/Containerfile" "${here}"

collect() {
    docker exec "${name}" journalctl --no-pager -o short-precise -u reprintbuddy.service -u reprintbuddy-update.service \
        >"${out}/logs/journal.txt" 2>&1 || true
    docker exec "${name}" cat /var/lib/reprintbuddy/update-status.json >"${out}/logs/update-status.json" 2>/dev/null || true
    docker rm -f "${name}" >/dev/null 2>&1 || true
}
trap collect EXIT

# systemd in a container: privileged, the cgroups of the host, its own /run.
docker run -d --name "${name}" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --tmpfs /run --tmpfs /run/lock \
    --add-host github.com:127.0.0.1 --add-host api.github.com:127.0.0.1 \
    -v "${here}:/e2e/system:ro" -v "${out}/build:/e2e/build:ro" -v "${out}/logs:/e2e/logs" \
    "${image}" >/dev/null

# Right after the start systemd may not listen yet ("offline"): ask again for a while; the boot itself may take
# up to 5 minutes before the run gives up.
state=""
for _ in $(seq 60); do
    state="$(docker exec "${name}" timeout 300 systemctl is-system-running --wait 2>/dev/null || true)"
    case "${state}" in
        "" | offline) sleep 1 ;;
        *) break ;;
    esac
done
echo "systemd in the container: ${state}"
case "${state}" in
    running | degraded) ;;
    *)
        docker exec "${name}" systemctl --failed --no-pager || true
        echo "::error::systemd did not come up in the container (${state})"
        exit 1
        ;;
esac
if [ "${state}" = "degraded" ]; then
    docker exec "${name}" systemctl --failed --no-pager || true
fi

docker exec "${name}" python3 /e2e/system/scenario.py --build /e2e/build --logs /e2e/logs
