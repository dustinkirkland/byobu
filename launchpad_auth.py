#!/usr/bin/env python3
"""
Authenticate with Launchpad API for byobu bug/merge proposal management.
This will open a browser for OAuth approval.
"""

from launchpadlib.launchpad import Launchpad
from launchpadlib.credentials import UnencryptedFileCredentialStore
import os

print("Setting up Launchpad API authentication...")
print("This will open a browser window for you to approve access.")
print("")

# Use a credential store in the byobu directory
cred_dir = os.path.expanduser("~/.cache/byobu-launchpad")
os.makedirs(cred_dir, exist_ok=True)

cred_file = os.path.join(cred_dir, "credentials")

try:
    launchpad = Launchpad.login_with(
        'byobu-cleanup-tool',
        'production',
        credential_store=UnencryptedFileCredentialStore(cred_file),
        version='devel'
    )

    print("")
    print("✅ Authentication successful!")
    print(f"Credentials stored in: {cred_dir}")
    print("")

    # Test by getting the byobu project
    byobu = launchpad.projects['byobu']
    print(f"✅ Successfully connected to project: {byobu.display_name}")
    print(f"   Project URL: {byobu.web_link}")
    print("")
    print("You're all set! I can now interact with Launchpad on your behalf.")

except Exception as e:
    print(f"❌ Error during authentication: {e}")
    print("Please try again or check your network connection.")
