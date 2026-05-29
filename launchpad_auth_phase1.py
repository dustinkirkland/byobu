#!/usr/bin/env python3
"""
Phase 1: Get Launchpad OAuth request token and print the authorization URL.
Run this, visit the URL, authorize, then run launchpad_auth_phase2.py.
"""
import os
import json
from launchpadlib.credentials import Credentials, Consumer
from launchpadlib.uris import LPNET_WEB_ROOT

APP_NAME = "byobu-cleanup-tool"
CACHE_DIR = os.path.expanduser("~/.cache/byobu-launchpad")
TOKEN_FILE = os.path.join(CACHE_DIR, "request_token.json")

os.makedirs(CACHE_DIR, exist_ok=True)

creds = Credentials(APP_NAME)
# get_request_token returns the full authorization URL and stores the request
# token internally in creds._request_token
auth_url = creds.get_request_token(web_root=LPNET_WEB_ROOT)

# Save consumer + request token for phase 2
with open(TOKEN_FILE, "w") as f:
    json.dump({
        "consumer_key": creds.consumer.key,
        "consumer_secret": creds.consumer.secret,
        "request_token_key": creds._request_token.key,
        "request_token_secret": creds._request_token.secret,
    }, f)
os.chmod(TOKEN_FILE, 0o600)

print(f"\nPlease visit this URL in your browser to authorize byobu-cleanup-tool:\n")
print(f"  {auth_url}\n")
print(f"After clicking 'Allow', run:")
print(f"  ! python3 /home/kirkland/src/byobu/launchpad_auth_phase2.py")
