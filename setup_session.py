"""
Run this ONCE on your laptop to generate the Instagram session for the server.

v2: tries the sessionid-cookie method FIRST, because Instagram's password
endpoint often rejects correct passwords when it suspects automation
(especially on new accounts). The browser cookie method sidesteps that
entirely — Instagram already trusts your browser's login.

How to get the sessionid cookie:
  1. In Chrome, log into instagram.com as the bot account (approve any
     "Was this you?" prompt in the app if asked).
  2. Press F12 -> "Application" tab -> left sidebar: Cookies ->
     https://www.instagram.com
  3. Find the row named `sessionid`, copy its Value (long string with % signs).
  4. Run:  python setup_session.py   and paste it when asked.

IMPORTANT: don't log out of instagram.com in that browser afterwards —
logging out invalidates the sessionid. Just close the tab.
"""
import base64
import getpass
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired


def save_and_print(cl: Client):
    # Verify using the DM inbox — the endpoint the bot actually needs.
    # (feed/timeline is gated much harder and 403s for browser-born sessions.)
    threads = cl.direct_threads(amount=1)
    print(f"DM inbox reachable ({len(threads)} thread(s) visible) ✓")
    session_path = Path("session.json")
    cl.dump_settings(session_path)
    b64 = base64.b64encode(session_path.read_bytes()).decode()
    print("\n✅ Login verified. session.json saved.")
    print("\nCopy everything between the lines into the IG_SESSION_B64 env var on Render:")
    print("-" * 60)
    print(b64)
    print("-" * 60)


def main():
    print("Method 1 (recommended): browser sessionid cookie")
    print("Method 2: username + password (often blocked by Instagram)\n")
    choice = input("Use sessionid cookie? [Y/n]: ").strip().lower()

    cl = Client()
    cl.delay_range = [1, 3]

    if choice in ("", "y", "yes"):
        sessionid = input("Paste sessionid cookie value: ").strip()
        try:
            cl.login_by_sessionid(sessionid)
        except Exception as e:
            print(f"\n❌ sessionid login failed: {e}")
            print("Most likely the cookie was copied incompletely, or you logged "
                  "out of that browser session. Re-copy and try again.")
            return
        save_and_print(cl)
        return

    # Password path (kept as fallback)
    username = input("Bot account username: ").strip()
    password = getpass.getpass("Bot account password: ")
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        code = input("2FA code: ").strip()
        cl.login(username, password, verification_code=code)
    except ChallengeRequired:
        print("\nInstagram raised a security challenge. Open the Instagram app, "
              "approve the login ('It was me'), wait a minute, retry. "
              "If it keeps failing, use the sessionid method instead.")
        return
    except Exception as e:
        print(f"\n❌ Password login failed: {e}")
        print("If you're sure the password is right, Instagram is blocking "
              "automated login. Use the sessionid cookie method instead.")
        return
    save_and_print(cl)


if __name__ == "__main__":
    main()