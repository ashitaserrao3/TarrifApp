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
import re
import zipfile

import requests

OBJECT = os.environ.get("SUPABASE_OBJECT", "published-tariffs.zip")


class SupabaseStore:
    def __init__(self, url, key, bucket="tariffs", obj=OBJECT):
        # Accept the project URL also as copied with an API path, e.g. https://x.supabase.co/rest/v1/
        base = re.sub(r"/(rest|storage)/v1(/.*)?$", "", url.strip().rstrip("/"))
        self.bucket = bucket.strip()
        self.bucket_url = f"{base}/storage/v1/bucket"
        self.url = f"{base}/storage/v1/object/{self.bucket}/{obj}"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    @staticmethod
    def _bucket_missing(r):
        return r.status_code in (400, 404) and "bucket not found" in r.text.lower()

    def create_bucket(self):
        """Create the private bucket (first run on a new Supabase project)."""
        r = requests.post(self.bucket_url, headers=self.headers, timeout=60,
                          json={"id": self.bucket, "name": self.bucket, "public": False})
        if r.status_code >= 400 and "already exists" not in r.text.lower():
            raise RuntimeError(f"Supabase bucket '{self.bucket}' is missing and couldn't be created "
                               f"(use the secret / service_role key): {r.status_code} {r.text[:200]}")

    def download(self):
        """The stored files {relative path: bytes}, or {} when nothing has been published yet."""
        r = requests.get(self.url, headers=self.headers, timeout=120)
        if self._bucket_missing(r):
            self.create_bucket()
            return {}  # new, empty bucket: nothing published yet
        if r.status_code in (400, 404) and "object not found" in r.text.lower():
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
        def post():
            return requests.post(self.url, headers={**self.headers, "Content-Type": "application/zip",
                                                    "x-upsert": "true"}, data=buf.getvalue(), timeout=300)
        r = post()
        if self._bucket_missing(r):
            self.create_bucket()
            r = post()
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase upload: {r.status_code} {r.text[:200]}")
