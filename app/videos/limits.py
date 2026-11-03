MIB = 1024 * 1024
MAX_SOURCE_BYTES = 500 * MIB
# 20 minutes at 2.5 Mbit video + 128 Kbit audio, container/poster margin.
OUTPUT_RESERVATION = 420 * MIB
USER_QUOTA = 5 * 1024 * MIB
DISK_FLOOR = 5 * 1024 * MIB
MAX_QUEUE = 20
MAX_UPLOADS = 2
RESERVATION_SECONDS = 300
UPLOAD_LEASE_SECONDS = 120
JOB_LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 10
MAX_ATTEMPTS = 3
# Serializes short capacity/accounting transitions across API processes and worker.
CAPACITY_LOCK = 91827061
