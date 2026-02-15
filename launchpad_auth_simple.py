#!/usr/bin/env python3
"""
Simple Launchpad authentication for remote/SSH sessions.
"""

from launchpadlib.launchpad import Launchpad
from launchpadlib.credentials import Credentials
from launchpadlib.uris import lookup_service_root
import os
import webbrowser

# Disable automatic browser opening
webbrowser.register('none', None, webbrowser.GenericBrowser('echo'), -1)
webbrowser._tryorder = ['none']

print("Setting up Launchpad API authentication...")
print()

# Use a credential store
cred_dir = os.path.expanduser("~/.cache/byobu-launchpad")
os.makedirs(cred_dir, exist_ok=True)

try:
    # This will print a URL for the user to visit
    launchpad = Launchpad.login_with(
        'byobu-cleanup-tool',
        'production',
        version='devel',
        credential_save_failed=lambda: None  # Don't fail if we can't save yet
    )

    print("\n✅ Authentication successful!")

    # Test the connection
    byobu = launchpad.projects['byobu']
    print(f"✅ Successfully connected to project: {byobu.display_name}")
    print("\nYou're all set! I can now interact with Launchpad on your behalf.")

except Exception as e:
    print(f"\n❌ Error during authentication: {e}")
    import traceback
    traceback.print_exc()
