import pytest

import supabase_store
from fake_supabase import KEY, FakeSupabase


@pytest.fixture
def supabase():
    s = FakeSupabase()
    yield s
    s.close()


def test_nothing_published_then_round_trips_and_overwrites(supabase):
    store = supabase_store.SupabaseStore(supabase.url, KEY)
    assert store.download() == {}  # nothing published yet
    store.upload({"rates/Indigo Eff. 01st Oct 2026.xlsx": b"old"})
    store.upload({"rates/Indigo Eff. 16th Oct 2026.xlsx": b"new", "rates/SG 29-Jul-26.xlsx": b"sg"})
    assert supabase.uploads == 2
    restarted = supabase_store.SupabaseStore(supabase.url + "/", KEY)  # e.g. after an app restart
    assert restarted.download() == {"rates/Indigo Eff. 16th Oct 2026.xlsx": b"new", "rates/SG 29-Jul-26.xlsx": b"sg"}


def test_wrong_key_is_reported(supabase):
    with pytest.raises(RuntimeError, match="Invalid key"):
        supabase_store.SupabaseStore(supabase.url, "wrong").download()


def test_missing_bucket_is_reported_not_taken_as_empty(supabase):
    with pytest.raises(RuntimeError, match="Bucket not found"):
        supabase_store.SupabaseStore(supabase.url, KEY, bucket="tarifs").download()


def test_project_url_copied_with_an_api_path_still_works(supabase):
    for url in (supabase.url + "/rest/v1/", supabase.url + "/rest/v1", supabase.url + "/storage/v1/ "):
        store = supabase_store.SupabaseStore(url, KEY)
        assert store.url == supabase.url + "/storage/v1/object/tariffs/published-tariffs.zip"
        assert store.download() == {}
