import os
import subprocess
import glob
import math
from concurrent.futures import ThreadPoolExecutor

# ================= CONFIGURATION =================
from config_local import VMS, LOCAL_INPUT_DIR, LOCAL_OUTPUT_DIR

# Remote Linux Paths on the VMs
REMOTE_INPUT = "/home/ubuntu/transcode/input"
REMOTE_OUTPUT = "/home/ubuntu/transcode/output"

# Video extensions to scan for
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".m4v")
# Minimum free space required on VM boot volume to accept a file (in GB)
MIN_VM_FREE_SPACE_GB = 15
# =================================================


def run_ssh(vm, command):
    """Executes a command on a remote VM via Windows built-in SSH client."""
    ssh_cmd = [
        "ssh",
        "-i",
        vm["key"],
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=5",
        f"{vm['user']}@{vm['ip']}",
        command,
    ]
    # Set shell=True on Windows to prevent console windows from popping up
    return subprocess.run(ssh_cmd, capture_output=True, text=True)


def get_vm_free_space_gb(vm):
    """Queries the VM for its available disk space on the root partition."""
    # df -BG returns sizes in Gigabytes. We extract the 'Available' column.
    res = run_ssh(vm, "df -BG / | tail -n 1 | awk '{print $4}'")
    if res.returncode == 0:
        try:
            # Output is usually something like "35G" or "35". We extract the number.
            clean = "".join(c for c in res.stdout.strip() if c.isdigit())
            return int(clean) if clean else 0
        except ValueError:
            return 0
    return 0


def list_remote_files(vm, directory):
    """Lists files in a remote directory."""
    res = run_ssh(vm, f"ls -1 {directory}")
    if res.returncode == 0:
        return [f.strip() for f in res.stdout.split("\n") if f.strip()]
    return []


def get_active_remote_basenames():
    """Scans all VMs to see which video basenames are already in progress."""
    active = set()
    for vm in VMS:
        print(f"Checking status of {vm['name']}...")
        for folder in [REMOTE_INPUT, REMOTE_OUTPUT]:
            files = list_remote_files(vm, folder)
            for f in files:
                base, _ = os.path.splitext(f)
                # If a transcode failed and was renamed to 'file.mp4.failed', clean the extension
                if base.endswith(".failed"):
                    base = os.path.splitext(base)[0]
                active.add(base.lower())
    return active


def upload_file(vm, local_path):
    """Uploads file directly to remote input directory."""
    filename = os.path.basename(local_path)
    remote_input_path = f"{REMOTE_INPUT}/{filename}"

    print(f"Uploading '{filename}' to {vm['name']}...")

    # Use Windows built-in scp
    scp_cmd = [
        "scp",
        "-i",
        vm["key"],
        "-o",
        "StrictHostKeyChecking=no",
        local_path,
        f"{vm['user']}@{vm['ip']}:{remote_input_path}",
    ]

    transfer = subprocess.run(scp_cmd)
    if transfer.returncode == 0:
        print(f"Successfully enqueued '{filename}' on {vm['name']}.")
        return True
    print(f"Failed to upload '{filename}' to {vm['name']}.")
    return False


def process_vm(vm, files_to_upload):
    """Handles the full lifecycle (download, space check, upload) for a single VM."""
    # ------------------ PHASE 1: DOWNLOAD COMPLETED ENCODES ------------------
    completed_files = list_remote_files(vm, REMOTE_OUTPUT)
    for filename in completed_files:
        if filename.startswith("."):
            continue

        remote_path = f"{REMOTE_OUTPUT}/{filename}"
        local_path = os.path.join(LOCAL_OUTPUT_DIR, filename)

        print(f"[{vm['name']}] Found completed file '{filename}'. Downloading...")
        scp_cmd = [
            "scp",
            "-i",
            vm["key"],
            "-o",
            "StrictHostKeyChecking=no",
            f"{vm['user']}@{vm['ip']}:{remote_path}",
            local_path,
        ]

        download = subprocess.run(scp_cmd, capture_output=True)
        if (
            download.returncode == 0
            and os.path.exists(local_path)
            and os.path.getsize(local_path) > 0
        ):
            print(f"[{vm['name']}] Download complete. Deleting remote copy...")
            run_ssh(vm, f'rm "{remote_path}"')

            # Find and delete local original raw video
            base_name, _ = os.path.splitext(filename)
            for ext in VIDEO_EXTENSIONS:
                local_original = os.path.join(LOCAL_INPUT_DIR, base_name + ext)
                if os.path.exists(local_original):
                    print(f"[{vm['name']}] Deleting local original: {local_original}")
                    os.remove(local_original)
                    break

    # ------------------ PHASE 2: UPLOAD NEW RAW VIDEOS ------------------
    if not files_to_upload:
        return

    space = get_vm_free_space_gb(vm)
    print(f"[{vm['name']}] Current free space: {space} GB.")

    for local_file in files_to_upload:
        filename = os.path.basename(local_file)
        file_size_gb = os.path.getsize(local_file) / (1024**3)
        required_space = max(MIN_VM_FREE_SPACE_GB, math.ceil(file_size_gb * 3))

        if space > required_space:
            success = upload_file(vm, local_file)
            if success:
                # Update local space tracking for this thread
                space -= math.ceil(file_size_gb * 2.5)
        else:
            print(
                f"[{vm['name']}] Skipping '{filename}' - insufficient space (needs {required_space} GB)."
            )


def main():
    if not os.path.exists(LOCAL_OUTPUT_DIR):
        os.makedirs(LOCAL_OUTPUT_DIR)

    print("--- Initializing Parallel Sync ---")

    # Check which files are already active on ANY VM to avoid duplicate uploads
    active_remotes = get_active_remote_basenames()

    # Scan for local candidates
    print("\n--- Scanning for New Local Videos ---")
    local_files = [
        f
        for f in glob.glob(os.path.join(LOCAL_INPUT_DIR, "*"))
        if os.path.isfile(f)
        and f.lower().endswith(VIDEO_EXTENSIONS)
        and os.path.splitext(os.path.basename(f))[0].lower() not in active_remotes
    ]

    if not local_files:
        print("No new local videos to process.")
    else:
        print(
            f"Found {len(local_files)} new files. Performing rough split across {len(VMS)} VMs."
        )

    # Perform the "Rough Split" (Round Robin)
    vm_assignments = [[] for _ in range(len(VMS))]
    for i, file_path in enumerate(local_files):
        vm_assignments[i % len(VMS)].append(file_path)

    # Run parallel tasks
    with ThreadPoolExecutor(max_workers=len(VMS)) as executor:
        for i, vm in enumerate(VMS):
            executor.submit(process_vm, vm, vm_assignments[i])

    print("\n--- All VM tasks submitted ---")


if __name__ == "__main__":
    main()
