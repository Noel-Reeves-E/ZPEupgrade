def send_email(subject, body):
    try:
        full_subject = f"[Nodegrid Upgrade] {subject}"
        result = subprocess.run(
            ["mailx", "-s", full_subject, "-r", EMAIL_FROM, EMAIL_TO],
            input=body, capture_output=True, text=True
        )
        print(f"    [EMAIL] {'Sent' if result.returncode == 0 else 'Failed'}: {full_subject}")
    except Exception as e:
        print(f"    [EMAIL] Could not send: {e}")

