#!/usr/bin/env python3
"""Set the scoreboard web-login username/password from the command line.

Writes a salted PBKDF2 hash to ``credentials.json`` (git-ignored, mode
0600). Run as the same user the scoreboard service runs as so the service
can read the file.
"""

import getpass
import sys

import credentials_store


def main():
    current = credentials_store.get_username()
    username = input("Username [%s]: " % current).strip() or current
    password = getpass.getpass("New password: ")
    if not password:
        sys.exit("Aborted: empty password.")
    if password != getpass.getpass("Verify password: "):
        sys.exit("Aborted: passwords do not match.")
    credentials_store.save_credentials(username, password)
    print("Credentials written to %s" % credentials_store.credentials_file)


if __name__ == "__main__":
    main()
