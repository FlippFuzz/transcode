#!/usr/bin/env bash

# Configuration - Adjust these paths as needed on your cloud instance
INPUT_DIR="/home/ubuntu/transcode/02_transcode_queue"
OUTPUT_DIR="/home/ubuntu/transcode/04_transcode_finished"
STAGING_DIR="/home/ubuntu/transcode/03_transcode_staging"
UPLOAD_STAGING_DIR="/home/ubuntu/transcode/01_upload_staging"
LOCK_FILE="/tmp/transcode_process.lock"
LOG_FILE="/home/ubuntu/transcode/transcode.log"
FFMPEG_BIN="/home/ubuntu/transcode/ffmpeg"
MAX_LOG_SIZE=5242880 # 5MB
UPDATE_INTERVAL=3600 # Check for updates once per hour (3600 seconds)
UPDATE_STAMP="/tmp/.transcode_update_stamp"
FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz"

# Ensure directories exist
mkdir -p "$INPUT_DIR" "$OUTPUT_DIR" "$STAGING_DIR" "$UPLOAD_STAGING_DIR"

# Log rotation: If log file exceeds MAX_LOG_SIZE, rotate it
if [ -f "$LOG_FILE" ] && [ $(stat -c%s "$LOG_FILE") -gt $MAX_LOG_SIZE ]; then
    rm -f "${LOG_FILE}.3"
    [ -f "${LOG_FILE}.2" ] && mv "${LOG_FILE}.2" "${LOG_FILE}.3"
    [ -f "${LOG_FILE}.1" ] && mv "${LOG_FILE}.1" "${LOG_FILE}.2"
    mv "$LOG_FILE" "${LOG_FILE}.1"
    echo "--- Log rotated: $(date) ---" > "$LOG_FILE"
fi

# Use flock to ensure only one instance of the script runs at a time.
# This prevents CPU over-saturation if a previous cron job is still processing.
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "Transcode process already running. Exiting." >> "$LOG_FILE"; exit 1; }

# --- Self-Update Logic ---
# Only check for updates if the interval has passed to be a good citizen to GitHub.
# Also trigger if FFmpeg is missing entirely.
if [ ! -f "$FFMPEG_BIN" ] || [ $(( $(date +%s) - $(stat -c %Y "$UPDATE_STAMP" 2>/dev/null || echo 0) )) -gt $UPDATE_INTERVAL ]; then
    touch "$UPDATE_STAMP"

    # 1. Update ffmpeg (BtbN builds are frequent and include SVT-AV1 improvements)
    # wget -N uses timestamping to only download if the remote file is newer than the local archive
    FFMPEG_DIR=$(dirname "$FFMPEG_BIN")
    ARCHIVE_PATH="$FFMPEG_DIR/ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
    
    if wget -qN "$FFMPEG_URL" -P "$FFMPEG_DIR"; then
        # If the archive is newer than the existing binary, extract and replace it
        if [ "$ARCHIVE_PATH" -nt "$FFMPEG_BIN" ]; then
            echo "--- New ffmpeg version detected. Updating binary... ---" >> "$LOG_FILE"
            tar -xf "$ARCHIVE_PATH" -C "$FFMPEG_DIR"
            mv "$FFMPEG_DIR/ffmpeg-master-latest-linuxarm64-gpl/bin/ffmpeg" "$FFMPEG_BIN"
            rm -rf "$FFMPEG_DIR/ffmpeg-master-latest-linuxarm64-gpl"
        fi
    fi

    # 2. Update the script itself from Git
    cd "$(dirname "$0")"
    if [ -d .git ]; then
        OLD_HASH=$(git rev-parse HEAD 2>/dev/null)
        # Reset local changes to ensure git pull succeeds without merge conflicts
        git reset --hard HEAD >> "$LOG_FILE" 2>&1
        if git pull >> "$LOG_FILE" 2>&1; then
            NEW_HASH=$(git rev-parse HEAD 2>/dev/null)
            if [ "$OLD_HASH" != "$NEW_HASH" ]; then
                chmod +x "$0" # Ensure the updated script is executable
                echo "--- Script updated from $OLD_HASH to $NEW_HASH. Restarting... ---" >> "$LOG_FILE"
                exec "$0" "$@"
            fi
        fi
    fi
fi

# Final safety check: If ffmpeg is still missing (e.g. download failed), exit
if [ ! -f "$FFMPEG_BIN" ]; then
    echo "ERROR: ffmpeg binary not found at $FFMPEG_BIN and update failed. Exiting." >> "$LOG_FILE"
    exit 1
fi

echo "--- Starting transcode session: $(date) ---" >> "$LOG_FILE"

for filepath in "$INPUT_DIR"/*; do
    # Check if there are any files to process (handles empty directory case)
    [ -e "$filepath" ] || continue

    filename=$(basename "$filepath")

    # Ignore hidden files (rsync) and WinSCP temporary files (.filepart)
    if [[ "$filename" == .* ]] || [[ "$filename" == *.filepart ]]; then
        continue
    fi

    # Prevent processing files currently being transferred (e.g., via standard scp)
    # If the file was modified less than 10 seconds ago, skip it for this cycle.
    last_mod=$(stat -c %Y "$filepath")
    if [ $(( $(date +%s) - last_mod )) -lt 10 ]; then
        continue
    fi

    echo "Processing: $filename" >> "$LOG_FILE"

    # Define staging and final paths. Extension is set to .mkv.
    staging_path="$STAGING_DIR/${filename%.*}.mkv"
    final_path="$OUTPUT_DIR/${filename%.*}.mkv"

    # Run ffmpeg with SVT-AV1 at low priority (nice) to keep the system responsive.
    # 'time' is included to log the duration of each transcode.
    # Example:
    # Test Input file: 3.8G
    # Preset 4 - about speed=0.25x, 1.6G
    # Preset 6 - about speed=0.479x, 1.7G
    # Chosen preset 3 because I am OK with slower speed
    nice -n 19 time "$FFMPEG_BIN" -stats_period 60 \
        -threads 4 -i "$filepath" -c:v libsvtav1 -preset 3 -crf 28 \
        -pix_fmt yuv420p10le -svtav1-params tune=0:scd=1:lp=4:keyint=10s -c:a libopus -b:a 128k \
        -y "$staging_path" 2>&1 | tr '\r' '\n' >> "$LOG_FILE"

    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        # Verification: Check if output file exists and is not empty
        if [ ! -s "$staging_path" ]; then
            echo "ERROR: Transcode finished but output file is missing or empty: $staging_path" >> "$LOG_FILE"
            rm -f "$staging_path"
            continue
        fi

        mv "$staging_path" "$final_path"
        echo "Successfully transcoded: $filename" >> "$LOG_FILE"
        # Only delete source file on success
        rm "$filepath"
    else
        echo "ERROR: Failed to transcode: $filename. See logs for details." >> "$LOG_FILE"
    fi
done

echo "--- Session finished: $(date) ---" >> "$LOG_FILE"