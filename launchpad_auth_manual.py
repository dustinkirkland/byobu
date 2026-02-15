#!/usr/bin/env python3
"""
Manual Launchpad authentication for remote/SSH sessions.
"""

from launchpadlib.launchpad import Launchpad
from launchpadlib.credentials import RequestTokenAuthorizationEngine, UnencryptedFileCredentialStore
import os

class ManualAuthorizationEngine(RequestTokenAuthorizationEngine):
    """Authorization engine that prints URL for manual approval."""

    def make_end_user_authorize_token(self, credentials, request_token):
        """Print the authorization URL for the user to visit."""
        auth_url = credentials.auth_engine.authorization_url(request_token)
        print("\n" + "="*70)
        print("AUTHORIZATION REQUIRED")
        print("="*70)
        print("\nPlease visit this URL in your browser:\n")
        print(f"    {auth_url}\n")
        print("After authorizing, Launchpad will display your credentials.")
        print("You don't need to paste anything back - just click 'Continue'")
        print("and press Enter here when done.")
        print("="*70 + "\n")
        input("Press Enter after you've authorized in the browser...")

print("Setting up Launchpad API authentication (manual mode)...")

# Use a credential store
cred_dir = os.path.expanduser("~/.cache/byobu-launchpad")
os.makedirs(cred_dir, exist_ok=True)
cred_file = os.path.join(cred_dir, "credentials")

try:
    service_root = 'production'
    launchpad = Launchpad.login_with(
        'byobu-cleanup-tool',
        service_root,
        credential_store=UnencryptedFileCredentialStore(cred_file),
        authorization_engine=ManualAuthorizationEngine(service_root),
        version='devel'
    )

    print("\n✅ Authentication successful!")
    print(f"Credentials stored in: {cred_file}")

    # Test the connection
    byobu = launchpad.projects['byobu']
    print(f"✅ Successfully connected to project: {byobu.display_name}")
    print("\nYou're all set! I can now interact with Launchpad on your behalf.")

except Exception as e:
    print(f"\n❌ Error during authentication: {e}")
    import traceback
    traceback.print_exc()
