"""
Create a bcrypt hash for a new/changed password.

    python generate_password.py

Type the password when asked (it is not shown or saved), then paste the
printed hash into Streamlit Secrets under that user's `password`.
"""

from getpass import getpass

import streamlit_authenticator as stauth

while True:
    pw = getpass("New password (blank to quit): ")
    if not pw:
        break
    if pw != getpass("Repeat password: "):
        print("Passwords did not match, try again.\n")
        continue
    print("\nHash:\n" + stauth.Hasher.hash(pw) + "\n")
