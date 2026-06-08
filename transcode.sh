#!/usr/bin/env bash

# Configuration - Adjust these paths as needed on your cloud instance
INPUT_DIR="/home/ubuntu/transcode/input"
OUTPUT_DIR="/home/ubuntu/transcode/output"
LOCK_FILE="/tmp/transcode_process.lock"
LOG_FILE="/home/ubuntu/transcode/transcode.log"
FFMPEG_BIN="/home/ubuntu/transcode/ffmpeg"
MAX_LOG_SIZE=5242880 # 5MB

# Ensure directories exist
mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"

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

    # Define the output path. Extension is set to .mkv.
    output_path="$OUTPUT_DIR/${filename%.*}.mkv"

    # Run ffmpeg with SVT-AV1 at low priority (nice) to keep the system responsive.
    # 'time' is included to log the duration of each transcode.
    # Example:
    # Test Input file: 3.8G
    # Preset 4 - about speed=0.25x, 1.6G - I am OK with the slower speed for 100MB size difference
    # Preset 6 - about speed=0.479x, 1.7G
    if nice -n 19 time "$FFMPEG_BIN" -threads 4 -i "$filepath" -c:v libsvtav1 -preset 4 -crf 28 \
        -pix_fmt yuv420p10le -svtav1-params tune=0:scd=1:lp=4 -c:a libopus -b:a 128k -y "$output_path" >> "$LOG_FILE" 2>&1; then
        echo "Successfully transcoded: $filename" >> "$LOG_FILE"
        # Only delete source file on success
        rm "$filepath"
    else
        echo "ERROR: Failed to transcode: $filename. See logs for details." >> "$LOG_FILE"
    fi
done

echo "--- Session finished: $(date) ---" >> "$LOG_FILE"