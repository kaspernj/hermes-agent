from datetime import datetime, timedelta, timezone

from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.continuation_lease import LeaseStore


def test_lease_fences_duplicate_then_allows_release_and_rotation(tmp_path):
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    leases = LeaseStore(tmp_path / "leases.json", ttl_seconds=60, clock=lambda: now)
    assert leases.acquire("route", "head-a", "delivery-1").admitted
    assert not leases.acquire("route", "head-a", "delivery-2").admitted
    leases.release("route", "head-a", "delivery-1", completed=True)
    assert leases.acquire("route", "head-a", "delivery-2").admitted
    assert leases.acquire("route", "head-b", "delivery-3").admitted


def test_expired_lease_is_observable_not_silently_stolen(tmp_path):
    current = [datetime(2026, 8, 13, tzinfo=timezone.utc)]
    leases = LeaseStore(tmp_path / "leases.json", ttl_seconds=5, clock=lambda: current[0])
    leases.acquire("route", "head", "d1")
    current[0] += timedelta(seconds=6)
    result = leases.acquire("route", "head", "d2")
    assert not result.admitted
    assert result.state == "stale"
    assert leases.status("route")["state"] == "stale"
    leases.recover_stale("route")
    assert leases.acquire("route", "head", "d2").admitted
