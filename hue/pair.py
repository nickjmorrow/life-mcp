"""Create an app key on the Hue Bridge (one time).

    uv run --project ../server pair.py >> ~/.zshrc.local    # then press the bridge's round button
    uv run --project ../server pair.py --find               # just print the bridge's IP

Prompts go to stderr, so only the two export lines reach stdout.
"""
import os
import sys
import time

import httpx

WAIT_S = 300  # keep asking this long, so the button can be pressed any time after starting


def find_ip() -> str:
    found = httpx.get("https://discovery.meethue.com", timeout=5).json()
    if not found:
        sys.exit("No Hue Bridge found. Is it on the same network as this Mac?")
    return found[0]["internalipaddress"]


def main() -> None:
    ip = os.environ.get("HUE_BRIDGE_IP") or find_ip()
    if "--find" in sys.argv:
        print(ip)
        return
    deadline = time.monotonic() + WAIT_S
    print("Press the round button on top of the Hue Bridge…", file=sys.stderr)
    while True:
        reply = httpx.post(
            f"https://{ip}/api",
            json={"devicetype": "life-mcp#hue", "generateclientkey": True},
            verify=False,  # self-signed bridge cert, LAN only
            timeout=5,
        ).json()[0]
        if "success" in reply:
            print(f"export HUE_BRIDGE_IP={ip}")
            print(f"export HUE_APP_KEY={reply['success']['username']}")
            print("Paired.", file=sys.stderr)
            return
        error = reply["error"]
        if error["type"] != 101 or time.monotonic() > deadline:  # 101 = button not pressed yet
            sys.exit(f"Pairing failed: {error['description']}")
        time.sleep(2)


if __name__ == "__main__":
    main()
