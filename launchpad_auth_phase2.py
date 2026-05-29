#!/usr/bin/env python3
"""
Phase 2: Exchange the authorized request token for an access token and save credentials.
Run this AFTER visiting the URL from launchpad_auth_phase1.py and clicking Allow.
"""
import os
import json
from launchpadlib.credentials import Credentials, Consumer, AccessToken
from launchpadlib.uris import LPNET_WEB_ROOT

APP_NAME = "byobu-cleanup-tool"
CACHE_DIR = os.path.expanduser("~/.cache/byobu-launchpad")
TOKEN_FILE = os.path.join(CACHE_DIR, "request_token.json")
CRED_FILE = os.path.join(CACHE_DIR, "credentials")

if not os.path.exists(TOKEN_FILE):
    print("ERROR: No request token found. Run launchpad_auth_phase1.py first.")
    raise SystemExit(1)

with open(TOKEN_FILE) as f:
    state = json.load(f)

# Reconstruct credentials with the saved consumer and request token
creds = Credentials(
    consumer_name=state["consumer_key"],
    consumer_secret=state["consumer_secret"],
)
creds._request_token = AccessToken(
    state["request_token_key"], state["request_token_secret"]
)

print("Exchanging request token for access token...")
try:
    creds.exchange_request_token_for_access_token(web_root=LPNET_WEB_ROOT)
except Exception as e:
    print(f"ERROR: {e}")
    print("Make sure you clicked 'Allow' in your browser before running this.")
    raise SystemExit(1)

creds.save_to_path(CRED_FILE)
os.chmod(CRED_FILE, 0o600)
os.remove(TOKEN_FILE)
print(f"✅ Credentials saved to {CRED_FILE}")
print("\nNow run:")
print("  ! python3 /home/kirkland/src/byobu/launchpad_close_bugs.py")
