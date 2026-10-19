"""
Keeps the tariffs Ashita publishes in a private Supabase Storage bucket, so they
stay after the app restarts until she publishes again, without storing them in GitHub.

The stored object is a zip of the app's data/rates folder.

Needs in Streamlit Secrets (see secrets.toml.example):
    [supabase] url = "https://<project>.supabase.co", key = "<secret / service_role key>",
               bucket = "tariffs"
"""

import io
import os
import zipfile

import requests

OBJECT = os.environ.get("SUPABASE_OBJECT", "published-tariffs.zip")


class SupabaseStore:
    def __init__(self, url, key, bucket="tariffs", obj=OBJECT):
        self.url = f"{url.rstrip('/')}/storage/v1/object/{bucket}/{obj}"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    def download(self):
        """The stored files {relative path: bytes}, or {} when nothing has been published yet."""
        r = requests.get(self.url, headers=self.headers, timeout=120)
        if r.status_code in (400, 404) and "not" in r.text.lower() and "found" in r.text.lower():
            return {}  # nothing published yet
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase download: {r.status_code} {r.text[:200]}")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            return {name: z.read(name) for name in z.namelist() if not name.endswith("/")}

    def upload(self, files):
        """Replace the stored files with {relative path: bytes}."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in sorted(files.items()):
                z.writestr(name, data)
        r = requests.post(self.url, headers={**self.headers, "Content-Type": "application/zip", "x-upsert": "true"},
                          data=buf.getvalue(), timeout=300)
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase upload: {r.status_code} {r.text[:200]}")
