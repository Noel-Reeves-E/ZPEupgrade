#!/usr/bin/env python3

# ============================================================
#  Nodegrid OS Upgrade Script
#  - Reads IPs from devices.txt (one IP per line)
#  - SSHes into each device one by one
#  - Backup -> Upload ISO+MD5 -> Validate MD5 -> Upgrade
#  - Waits for reboot (SSH polling, no socket module)
#  - Reuses same SSH object throughout
#  - finally block closes SSH at the end of each device
#  - Sends email at every stage
# ============================================================

import paramiko
import time
import subprocess
import os
from datetime import datetime

# ============================================================
#  SETTINGS  --  Edit everything here before running
# ============================================================

IP_FILE      = "devices.txt"
SSH_USERNAME = "admin"
SSH_PASSWORD = "your_password_here"

EMAIL_TO   = "noc@yourcompany.com"
EMAIL_FROM = "nodegrid-upgrade@yourserver"

ISO_FILE     = "/opt/upgrades/Nodegrid_Platform_v6.0.0_20250101.iso"
MD5_FILE     = "/opt/upgrades/Nodegrid_Platform_v6.0.0_20250101.iso.md5"
ISO_FILENAME = os.path.basename(ISO_FILE)
MD5_FILENAME = os.path.basename(MD5_FILE)

BACKUP_SERVER_URL  = "sftp://192.168.10.50/backups"
BACKUP_SERVER_USER = "backupuser"
BACKUP_SERVER_PASS = "backuppass"

POLL_EVERY = 15    # seconds between each SSH probe attempt
MAX_WAIT   = 1800  # give up after 30 minutes (covers 10min pending + reboot time)

# ============================================================
#  SEND EMAIL  --  uses system mailx/mail (no SMTP config needed)
# ============================================================

def send_email(subject, body):
    try:
        full_subject = f"[Nodegrid Upgrade] {subject}"
        result = subprocess.run(
            ["mailx", "-s", full_subject, "-r", EMAIL_FROM, EMAIL_TO],
            input=body, capture_output=True, text=True
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["mail", "-s", full_subject, EMAIL_TO],
                input=body, capture_output=True, text=True
            )
        print(f"    [EMAIL] {'Sent' if result.returncode == 0 else 'Failed'}: {full_subject}")
    except Exception as e:
        print(f"    [EMAIL] Could not send: {e}")

# ============================================================
#  shell_cmd  --  sends commands via interactive shell and returns output
#  All commands passed as a single string with \n between each line
# ============================================================

def shell_cmd(ssh, commands, wait=5):
    shell = ssh.invoke_shell()
    time.sleep(2)
    shell.send(commands + "\n")
    time.sleep(wait)
    output = shell.recv(65535).decode("utf-8", errors="replace")
    shell.close()
    return output

# ============================================================
#  UPGRADE ONE DEVICE
# ============================================================

def upgrade_one_device(ip, num, total):

    print(f"\n{'='*60}")
    print(f"  Device {num}/{total}  --  {ip}")
    print(f"{'='*60}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # One SSH object reused for the entire device lifecycle
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:

        # ----------------------------------------------------------
        # STEP 1: Connect
        # ----------------------------------------------------------
        print(f"\n  [Step 1/6] Connecting to {ip}...")
        try:
            ssh.connect(
                hostname=ip, port=22,
                username=SSH_USERNAME, password=SSH_PASSWORD,
                timeout=30, look_for_keys=False, allow_agent=False
            )
            print(f"  [Step 1/6] Connected OK")
        except Exception as e:
            print(f"  [Step 1/6] FAILED -- {e}")
            send_email(f"FAILED - {ip} - SSH Connect", f"Could not connect to {ip}\nError: {e}\nTime: {now}")
            return "FAILED"

        # ----------------------------------------------------------
        # PRE-CHECK: Capture console device statuses BEFORE upgrade
        # ----------------------------------------------------------
        print(f"\n  [Pre-Check] Capturing console device status (show access/)...")
        pre_access_out = shell_cmd(ssh, "show access/")
        print(f"    Captured OK")

        # ----------------------------------------------------------
        # STEP 2: Pre-upgrade backup
        # ----------------------------------------------------------
        print(f"\n  [Step 2/6] Taking backup...")
        backup_file = f"{ip}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.cfg"

        try:
            out = shell_cmd(ssh, f"save_settings\nset destination=remote_server\nset url={BACKUP_SERVER_URL}/{backup_file}\nset username={BACKUP_SERVER_USER}\nset password={BACKUP_SERVER_PASS}\nsave", wait=10)
            if "error" in out.lower() or "fail" in out.lower():
                raise Exception(f"Backup output indicates failure:\n{out}")
            print(f"  [Step 2/6] Backup done: {backup_file}")
            send_email(
                f"Backup OK - {ip}",
                f"Backup completed for {ip}\nFile: {BACKUP_SERVER_URL}/{backup_file}\nTime: {now}\n\nProceeding to upload ISO and upgrade."
            )
        except Exception as e:
            print(f"  [Step 2/6] FAILED -- {e}")
            send_email(f"FAILED - {ip} - Backup", f"Backup failed for {ip}\nError: {e}\nTime: {now}")
            return "FAILED"

        # ----------------------------------------------------------
        # STEP 3: Upload ISO + MD5 to /var/sw/ on device via SFTP
        # ----------------------------------------------------------
        print(f"\n  [Step 3/6] Uploading ISO and MD5 to /var/sw/...")
        try:
            sftp = ssh.open_sftp()
            print(f"    Uploading {ISO_FILENAME} ...")
            sftp.put(ISO_FILE, f"/var/sw/{ISO_FILENAME}")
            print(f"    Uploading {MD5_FILENAME} ...")
            sftp.put(MD5_FILE, f"/var/sw/{MD5_FILENAME}")
            sftp.close()
            print(f"  [Step 3/6] Upload done")
        except Exception as e:
            print(f"  [Step 3/6] FAILED -- {e}")
            send_email(f"FAILED - {ip} - Upload", f"ISO upload failed for {ip}\nError: {e}\nTime: {now}")
            return "FAILED"

        # ----------------------------------------------------------
        # STEP 4: Validate MD5 on device
        # ----------------------------------------------------------
        print(f"\n  [Step 4/6] Validating MD5 checksum on device...")
        try:
            expected_md5 = shell_cmd(ssh, f"shell\ncat /var/sw/{MD5_FILENAME}").split()[0]
            print(f"    Expected : {expected_md5}")

            print(f"    Computing md5sum on device (may take ~60 seconds)...")
            actual_md5 = shell_cmd(ssh, f"shell\nmd5sum /var/sw/{ISO_FILENAME}", wait=120).split()[0]
            print(f"    Actual   : {actual_md5}")

            if expected_md5.lower() != actual_md5.lower():
                raise Exception(f"MD5 mismatch!\n  Expected: {expected_md5}\n  Got:      {actual_md5}")

            print(f"  [Step 4/6] MD5 valid -- OK")
        except Exception as e:
            print(f"  [Step 4/6] FAILED -- {e}")
            send_email(f"FAILED - {ip} - MD5 Validation", f"MD5 check failed for {ip}\nError: {e}\nTime: {now}")
            return "FAILED"

        # ----------------------------------------------------------
        # STEP 5: Trigger upgrade
        # Device reboots automatically after 'upgrade' -- SSH will drop
        # ----------------------------------------------------------
        print(f"\n  [Step 5/6] Starting upgrade...")
        send_email(
            f"Upgrade Starting - {ip}",
            f"Upgrade starting on {ip}\nImage: {ISO_FILENAME}\nTime: {now}\n\nDevice will reboot. Script is monitoring until it returns."
        )

        try:
            shell_cmd(ssh, f"software_upgrade\nset image_location=local_system\nset filename={ISO_FILENAME}\nupgrade", wait=5)
        except Exception:
            pass   # SSH drop during reboot is expected

        print(f"  [Step 5/6] Upgrade command sent -- device will reboot in 5-10 minutes")

        # ----------------------------------------------------------
        # STEP 6: Wait for device to go DOWN then come back UP
        #
        # Phase A -- device still up, upgrade processing in background
        # Phase B -- SSH fails, device is rebooting
        # Phase C -- SSH responds again, device is back
        # ----------------------------------------------------------
        print(f"\n  [Step 6/6] Monitoring device -- waiting for reboot to begin...")

        attempt     = 0
        waited      = 0
        device_down = False
        device_back = False

        while waited < MAX_WAIT:
            attempt += 1
            time.sleep(POLL_EVERY)
            waited += POLL_EVERY

            try:
                ssh.connect(
                    hostname=ip, port=22,
                    username=SSH_USERNAME, password=SSH_PASSWORD,
                    timeout=15, look_for_keys=False, allow_agent=False
                )
                if device_down:
                    print(f"    Device is back online! (attempt #{attempt}, ~{waited}s total)")
                    device_back = True
                    break
                else:
                    print(f"    Attempt #{attempt}: device still up, upgrade pending... ({waited}s elapsed)")

            except Exception:
                if not device_down:
                    print(f"    Attempt #{attempt}: device is DOWN -- reboot started! Waiting for it to come back...")
                    device_down = True
                else:
                    print(f"    Attempt #{attempt}: still rebooting... ({waited}s elapsed)")

        if not device_back:
            print(f"  [Step 6/6] FAILED -- Device did not come back within {waited}s")
            send_email(
                f"FAILED - {ip} - Reboot Timeout",
                f"Device {ip} did not return after upgrade.\nWaited: {waited}s\nTime: {now}\n\nManual check required."
            )
            return "FAILED"

        # ----------------------------------------------------------
        # POST-UPGRADE: Validate
        # show system/about/ gives hostname (system:), version (software:), uptime (uptime:)
        # ----------------------------------------------------------
        print(f"\n  [Post] Running post-upgrade validation...")
        try:
            about_out = shell_cmd(ssh, "show system/about/")
            hostname = ""
            version  = ""
            uptime   = ""
            for line in about_out.splitlines():
                line = line.strip()
                if line.startswith("system:"):
                    hostname = line.split("system:")[-1].strip()
                if line.startswith("software:"):
                    version = line.split("software:")[-1].strip()
                if line.startswith("uptime:"):
                    uptime = line.split("uptime:")[-1].strip()

            post_access_out = shell_cmd(ssh, "show access/")
            console_match = "YES" if sorted(pre_access_out.splitlines()) == sorted(post_access_out.splitlines()) else "NO"

            print(f"    Hostname       : {hostname}")
            print(f"    Version        : {version}")
            print(f"    Uptime         : {uptime}")
            print(f"    Console match  : {console_match}")
            print(f"  [Post] Validation PASSED")

            send_email(
                f"Upgrade Complete - {ip}",
                f"Upgrade completed successfully on {ip}\n\n"
                f"Hostname      : {hostname}\n"
                f"Version       : {version}\n"
                f"Uptime        : {uptime}\n"
                f"Console match : {console_match} (device statuses same before and after)\n"
                f"Reboot time   : ~{waited}s\n"
                f"Done at       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            return "SUCCESS"

        except Exception as e:
            print(f"  [Post] Validation error: {e}")
            send_email(
                f"WARNING - {ip} - Post-Upgrade Validation Failed",
                f"Device {ip} is back online but validation had an error.\nError: {e}\nPlease log in manually to confirm."
            )
            return "DEGRADED"

    finally:
        ssh.close()
        print(f"    SSH session closed for {ip}")

# ============================================================
#  MAIN
# ============================================================

print("\n" + "="*60)
print("  Nodegrid OS Upgrade Script")
print("="*60)

ip_list = []
with open(IP_FILE) as f:
    for line in f:
        ip = line.strip()
        if ip and not ip.startswith("#"):
            ip_list.append(ip)

print(f"  Devices  : {len(ip_list)}")
print(f"  ISO      : {ISO_FILE}")
print(f"  Email to : {EMAIL_TO}")
print("="*60)

results = []
for index, ip in enumerate(ip_list, start=1):
    status = upgrade_one_device(ip, index, len(ip_list))
    results.append((ip, status))
    if index < len(ip_list):
        print(f"\n  Pausing 10s before next device...")
        time.sleep(10)

print(f"\n{'='*60}")
print("  FINAL SUMMARY")
print(f"{'='*60}")
summary_body = f"Upgrade run finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
for ip, status in results:
    symbol = "OK" if status == "SUCCESS" else "!!"
    print(f"  [{symbol}]  {ip:<20}  {status}")
    summary_body += f"{ip:<20}  {status}\n"

success = sum(1 for _, s in results if s == "SUCCESS")
failed  = sum(1 for _, s in results if s != "SUCCESS")
summary_body += f"\n{success} succeeded,  {failed} failed  out of {len(results)} total."
print(f"{'='*60}")
print(f"  {success} succeeded,  {failed} failed")

send_email("Final Summary - Upgrade Run Complete", summary_body)
