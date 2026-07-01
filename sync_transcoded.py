import os
import subprocess
import glob
import math
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from fabric import Connection, Config
from paramiko import common
from paramiko import AutoAddPolicy

# ================= CONFIGURATION =================
from config_local import VMS, LOCAL_INPUT_DIR, LOCAL_OUTPUT_DIR

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
# Silence verbose Paramiko logs
logging.getLogger("paramiko").setLevel(logging.WARNING)

# Optimize Paramiko for high-latency/high-bandwidth connections (WinSCP-like behavior)
# These settings increase the amount of data in flight (like WinSCP does)
common.DEFAULT_WINDOW_SIZE = 62500000  # 62.5MB (Twice the BDP for 1Gbps @ 250ms)
common.DEFAULT_MAX_PACKET_SIZE = 32768  # 32KB (Standard SFTP packet limit)

# Remote Linux Paths on the VMs
REMOTE_INPUT = "/home/ubuntu/transcode/02_transcode_queue"
REMOTE_OUTPUT = "/home/ubuntu/transcode/04_transcode_finished"
REMOTE_STAGING_IN = "/home/ubuntu/transcode/01_upload_staging"
REMOTE_STAGING_OUT = "/home/ubuntu/transcode/03_transcode_staging"

WINSCP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "winscp.com"))

# Video extensions to scan for
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".m4v")
# Minimum free space required on VM boot volume to accept a file (in GB)
MIN_VM_FREE_SPACE_GB = 15
# =================================================


def get_connection(vm) -> Connection:
    """Establishes a Fabric Connection."""
    # Create a Config object to set the missing host key policy
    config = Config()
    # AutoAddPolicy is equivalent to StrictHostKeyChecking=no
    # This ensures that unknown host keys are automatically added to known_hosts.
    config.missing_host_key_policy = AutoAddPolicy()

    return Connection(
        host=vm["ip"],
        user=vm["user"],
        connect_kwargs={"key_filename": vm["key"]},
        connect_timeout=10,
        config=config,  # Pass the custom config
    )


def get_vm_free_space_gb(conn, vm_name):
    """Queries the VM for its available disk space on the root partition."""
    try:
        # hide=True prevents output from leaking to stdout; warn=True prevents exceptions on non-zero exit
        result = conn.run("df -BG --output=avail /", hide=True, warn=True)
        if not result.ok:
            return 0
        output = result.stdout
    except Exception as e:
        logging.warning(f"[{vm_name}] Space check failed: {e}")
        return 0

    for line in output.splitlines():
        line = line.strip()
        clean = "".join(c for c in line if c.isdigit())
        if clean:
            return int(clean)
    return 0


def list_remote_files(sftp, directory):
    """Lists files in a remote directory using SFTP."""
    try:
        return sftp.listdir(directory)
    except IOError:
        # Directory might not exist or be empty
        return []


def get_remote_status():
    """Scans all VMs to categorize files and measure current transcoding load."""
    active_all = set()
    staged_map = {}  # {vm_ip: {basenames}}
    vm_loads = {}  # {vm_ip: active_job_count}

    for vm in VMS:
        logging.info(f"Checking status of {vm['name']}...")
        ip = vm["ip"]
        staged_map[ip] = set()
        vm_loads[ip] = 0
        try:
            with get_connection(vm) as conn:
                sftp = conn.sftp()

                # 1. Input queue and active transcode folders count towards current VM load
                for folder in [REMOTE_INPUT, REMOTE_STAGING_OUT]:
                    files = list_remote_files(sftp, folder)
                    valid_files = [f for f in files if not f.startswith(".")]

                    if folder == REMOTE_INPUT:
                        logging.info(
                            f"[{vm['name']}] Queued jobs: {', '.join(valid_files) if valid_files else 'None'}"
                        )

                    # Accumulate load count
                    vm_loads[ip] += len(valid_files)

                    for f in valid_files:
                        base, _ = os.path.splitext(f)
                        active_all.add(base.lower())

                # 2. Finished files are active (to prevent re-uploading), but do not count as transcode load
                completed_files = list_remote_files(sftp, REMOTE_OUTPUT)
                for f in completed_files:
                    if not f.startswith("."):
                        base, _ = os.path.splitext(f)
                        active_all.add(base.lower())

                # 3. Check inbound staging for interrupted uploads
                staging_files = list_remote_files(sftp, REMOTE_STAGING_IN)
                for f in staging_files:
                    if not f.startswith("."):
                        base, _ = os.path.splitext(f)
                        staged_map[ip].add(base.lower())
        except Exception as e:
            logging.error(f"Could not connect to {vm['name']}: {e}")

    return active_all, staged_map, vm_loads


def run_winscp_command(vm, command_str):
    """Executes a command via WinSCP and redirects output to a local log file."""
    # WinSCP expects a specific connection string format.
    fingerprint = "*"  # Use wildcard to accept any host key in script mode
    connection_url = f"sftp://{vm['user']}@{vm['ip']}/"

    # 1. Check if the engine (winscp.exe) exists. .com needs .exe to function.
    winscp_engine = WINSCP_PATH.replace(".com", ".exe")
    if not os.path.exists(winscp_engine):
        logging.error(
            f"[{vm['name']}] Error: winscp.exe not found in {os.path.dirname(WINSCP_PATH)}"
        )
        return False

    # 2. Resolve the key path.
    # We only accept .ppk keys for WinSCP.
    key_path = vm.get("key_ppk")
    if not key_path:
        logging.error(f"[{vm['name']}] Error: 'key_ppk' is required for WinSCP.")
        return False
    resolved_key = os.path.abspath(os.path.expanduser(str(key_path)))

    if not os.path.exists(resolved_key):
        logging.error(f"[{vm['name']}] Error: Private key not found: {resolved_key}")
        return False

    log_path = os.path.join(os.path.dirname(__file__), f"winscp_{vm['name']}.log")
    winscp_cmd = [
        WINSCP_PATH,
        "/ini=nul",  # Use a temporary configuration to ensure script portability
        "/rawconfig",
        "Interface\\FlushConsole=1",  # Force WinSCP to bypass CRT buffering
    ]

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            with subprocess.Popen(
                winscp_cmd,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                bufsize=0,
            ) as proc:
                # Pass commands via stdin to avoid Windows command-line quoting/escaping issues
                if proc.stdin:
                    commands = [
                        f'open {connection_url} -privatekey="{resolved_key}" -hostkey={fingerprint}',
                        "option batch abort",
                        "option confirm off",
                        command_str,
                        "exit",
                        "",
                    ]
                    proc.stdin.write(("\n".join(commands) + "\n").encode("utf-8"))
                    proc.stdin.flush()
                    proc.stdin.close()

                proc.wait()
                if proc.returncode != 0:
                    logging.error(
                        f"[{vm['name']}] WinSCP failed (Code {proc.returncode}). Check {os.path.basename(log_path)}"
                    )
                    return False
            return True
    except Exception as e:
        logging.error(f"[{vm['name']}] WinSCP Execution Error: {e}")
        return False


def upload_file(conn, sftp, vm, local_path, vm_index):
    """Uploads file to staging with resumption, then moves it to input atomically."""
    filename = os.path.basename(local_path)
    vm_name = vm["name"]
    remote_staging_path = f"{REMOTE_STAGING_IN}/{filename}"
    remote_input_path = f"{REMOTE_INPUT}/{filename}"

    logging.info(f"Uploading '{filename}' to {vm_name} (staging)...")

    try:
        # Resumption logic: Check if remote file exists and get its size
        remote_size = 0
        try:
            remote_stat = sftp.stat(remote_staging_path)
            remote_size = remote_stat.st_size
        except IOError:
            pass  # File doesn't exist yet

        local_size = os.path.getsize(local_path)

        if remote_size > local_size:
            logging.warning(
                f"[{vm_name}] Remote staging file larger than local. Overwriting."
            )
            remote_size = 0

        if remote_size < local_size:
            # Use native absolute paths for WinSCP via stdin
            winscp_local_path = os.path.abspath(local_path)

            # WinSCP "put -resume" handles the logic of checking offset and appending.
            cmd = f'put -resume "{winscp_local_path}" "{remote_staging_path}"'
            if not run_winscp_command(vm, cmd):
                return False  # Abort if transfer failed

        # Verification step: Check remote size after upload
        try:
            remote_stat = sftp.stat(remote_staging_path)
            if remote_stat.st_size != local_size:
                logging.error(
                    f"[{vm_name}] Upload size mismatch for '{filename}': Local={local_size}, Remote={remote_stat.st_size}"
                )
                return False
        except Exception as e:
            logging.error(f"[{vm_name}] Failed to verify uploaded file size: {e}")
            return False

        logging.info(f"[{vm_name}] Transfer verified. Moving to input folder...")
        mv_res = conn.run(
            f'mv "{remote_staging_path}" "{remote_input_path}"', hide=True, warn=True
        )
        if mv_res.ok:
            logging.info(f"[{vm_name}] Successfully enqueued '{filename}'.")
            return True
        else:
            logging.error(f"[{vm_name}] Error moving file: {mv_res.stderr}")
    except Exception as e:
        logging.error(f"Failed to upload '{filename}' to {vm_name}: {e}")
    return False


def process_vm(vm, files_to_upload, vm_index):
    """Handles the full lifecycle (download, space check, upload) for a single VM."""
    try:
        with get_connection(
            vm
        ) as conn:  # Connection context manager handles client setup
            sftp = conn.sftp()

            # ------------------ PHASE 1: DOWNLOAD COMPLETED ENCODES ------------------
            completed_files = list_remote_files(sftp, REMOTE_OUTPUT)
            for filename in completed_files:
                if filename.startswith("."):
                    continue

                remote_path = f"{REMOTE_OUTPUT}/{filename}"
                local_path = os.path.join(LOCAL_OUTPUT_DIR, filename)

                try:
                    remote_size = sftp.stat(remote_path).st_size

                    # Locate the local original raw video
                    base_name, _ = os.path.splitext(filename)
                    local_original = None
                    for ext in VIDEO_EXTENSIONS:
                        potential_original = os.path.join(
                            LOCAL_INPUT_DIR, base_name + ext
                        )
                        if os.path.exists(potential_original):
                            local_original = potential_original
                            break

                    # OPTIMIZATION: If the remote file size is greater than or equal to our local original,
                    # the transcode did not save space (or the server kept the original).
                    # We can skip the download completely and relocate the local original.
                    if local_original and remote_size >= os.path.getsize(
                        local_original
                    ):
                        logging.info(
                            f"[{vm['name']}] '{filename}' was not optimized (original is smaller or equal). Skipping download, relocating local copy..."
                        )
                        shutil.move(local_original, local_path)

                        logging.info(f"[{vm['name']}] Deleting remote copy...")
                        sftp.remove(remote_path)
                        continue

                    # Fallback to normal download logic if sizes do not match or original isn't found
                    local_size = (
                        os.path.getsize(local_path) if os.path.exists(local_path) else 0
                    )

                    if local_size > remote_size:
                        logging.warning(
                            f"[{vm['name']}] Local file larger than remote. Restarting download."
                        )
                        local_size = 0

                    if local_size < remote_size:
                        logging.info(f"[{vm['name']}] Downloading '{filename}'...")
                        # Use native absolute paths for WinSCP via stdin
                        winscp_local_path = os.path.abspath(local_path)

                        # WinSCP "get -resume" for fast, resumable downloads
                        cmd = f'get -resume "{remote_path}" "{winscp_local_path}"'
                        if not run_winscp_command(vm, cmd):
                            continue  # Skip verification/cleanup if download failed

                    # Verification step: Check local size after download
                    try:
                        actual_local_size = (
                            os.path.getsize(local_path)
                            if os.path.exists(local_path)
                            else 0
                        )
                        if actual_local_size != remote_size:
                            logging.error(
                                f"[{vm['name']}] Download size mismatch for '{filename}': Remote={remote_size}, Local={actual_local_size}"
                            )
                            continue
                    except Exception as e:
                        logging.error(
                            f"[{vm['name']}] Failed to verify downloaded file size: {e}"
                        )
                        continue

                    logging.info(
                        f"[{vm['name']}] Download verified. Deleting remote copy..."
                    )
                    sftp.remove(remote_path)

                    # Delete the original raw video since it was successfully transcoded & optimized
                    if local_original and os.path.exists(local_original):
                        logging.info(
                            f"[{vm['name']}] Deleting local original: {local_original}"
                        )
                        os.remove(local_original)
                except Exception as e:
                    logging.error(f"[{vm['name']}] Error downloading {filename}: {e}")

            # ------------------ PHASE 2: UPLOAD NEW RAW VIDEOS ------------------
            if not files_to_upload:
                return

            space = get_vm_free_space_gb(conn, vm["name"])
            logging.info(f"[{vm['name']}] Current free space: {space} GB.")

            for local_file in files_to_upload:
                filename = os.path.basename(local_file)
                file_size_gb = os.path.getsize(local_file) / (1024**3)
                required_space = max(MIN_VM_FREE_SPACE_GB, math.ceil(file_size_gb * 3))

                if space > required_space:
                    success = upload_file(conn, sftp, vm, local_file, vm_index)
                    if success:
                        # Update local space tracking for this thread
                        space -= math.ceil(file_size_gb * 2.5)
                else:
                    logging.info(
                        f"[{vm['name']}] Skipping '{filename}' - insufficient space (needs {required_space} GB)."
                    )
    except Exception as e:
        logging.error(f"[{vm['name']}] Connection or Task failed: {e}")


def main():
    if not os.path.exists(LOCAL_OUTPUT_DIR):
        os.makedirs(LOCAL_OUTPUT_DIR)

    logging.info("--- Initializing Parallel Sync ---")

    # Get remote status to avoid duplicates, find interrupted uploads, and measure active loads
    active_remotes, staged_map, vm_loads = get_remote_status()

    # Scan for local candidates
    logging.info("--- Scanning for New Local Videos ---")
    all_local_files = [
        f
        for f in glob.glob(os.path.join(LOCAL_INPUT_DIR, "*"))
        if os.path.isfile(f) and f.lower().endswith(VIDEO_EXTENSIONS)
    ]

    # Filter out files already in Input or Output on any VM
    files_to_process = [
        f
        for f in all_local_files
        if os.path.splitext(os.path.basename(f))[0].lower() not in active_remotes
    ]

    # Print files to process
    if files_to_process:
        logging.info("Local files to process:")
        for f in files_to_process:
            logging.info(f" - {os.path.basename(f)}")

    if not files_to_process:
        logging.info("No new local videos to process.")
    else:
        logging.info(f"Found {len(files_to_process)} candidates. Assigning to VMs...")

    # Load-balanced Assignment Logic
    vm_assignments = [[] for _ in range(len(VMS))]
    current_loads = [vm_loads.get(vm["ip"], 0) for vm in VMS]
    unassigned = []

    for file_path in files_to_process:
        basename = os.path.splitext(os.path.basename(file_path))[0].lower()
        assigned = False
        # If file was already in staging on a specific VM, resume it there
        for i, vm in enumerate(VMS):
            if basename in staged_map[vm["ip"]]:
                vm_assignments[i].append(file_path)
                current_loads[
                    i
                ] += 1  # Resumed transfer adds to this VM's future processing load
                assigned = True
                logging.info(
                    f"Resuming interrupted upload of '{basename}' on {vm['name']}."
                )
                break
        if not assigned:
            unassigned.append(file_path)

    # Distribute remaining files to the VM with the lowest current load
    for file_path in unassigned:
        min_idx = current_loads.index(min(current_loads))
        vm_assignments[min_idx].append(file_path)
        current_loads[min_idx] += 1
        logging.info(
            f"Assigned '{os.path.basename(file_path)}' to {VMS[min_idx]['name']} (Estimated Load: {current_loads[min_idx]})."
        )

    # Run parallel tasks
    with ThreadPoolExecutor(max_workers=len(VMS)) as executor:
        for i, vm in enumerate(VMS):
            executor.submit(process_vm, vm, vm_assignments[i], i)

    logging.info("--- All VM tasks submitted ---")


if __name__ == "__main__":
    main()
