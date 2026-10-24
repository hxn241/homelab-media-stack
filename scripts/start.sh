#!/bin/bash
# Server startup: waits for the drives to be mounted, applies system tuning
# (optional) and starts the containers. Intended to run from @reboot.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; source "$ROOT/.env"; set +a

# Mount points to wait for (space-separated; defaults to MEDIA_ROOT)
read -ra MOUNT_POINTS <<< "${WAIT_FOR_MOUNTS:-$MEDIA_ROOT}"

# Maximum wait time (seconds)
MAX_WAIT=120

echo "Waiting for drives to be mounted..."

for mount_point in "${MOUNT_POINTS[@]}"; do
    waited=0

    # Wait until the mount point is available
    while ! mountpoint -q "$mount_point"; do
        if [ "$waited" -ge "$MAX_WAIT" ]; then
            echo "ERROR: mount point $mount_point is not available after $MAX_WAIT seconds."
            exit 1
        fi

        echo "Not mounted yet: $mount_point. Retrying..."
        sleep 2
        waited=$((waited + 2))
    done

    echo "Mounted: $mount_point"
done

echo "All drives are mounted."

# Apply system tuning
if [ "${RUN_OPTIMIZE:-false}" = "true" ]; then
    echo "Applying server optimizations..."
    "$ROOT/scripts/optimize.sh"
fi

echo "Starting Docker services..."

docker compose --project-directory "$ROOT" up -d

echo "Docker services started."

# Wait, then show container status
echo "Checking services..."
sleep 10
docker ps --format "table {{.Names}}\t{{.Status}}"
