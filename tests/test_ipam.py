import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipam import (
    ActiveLeaseError,
    AddressExhaustedError,
    AddressUnavailableError,
    IPAMEngine,
    IPAMError,
    LeaseNotFoundError,
    PoolNotFoundError,
    PoolOverlapError,
)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def engine(clock):
    return IPAMEngine(clock=clock)


# ---------------------------------------------------------------------
# Pool management: IPv4 / IPv6 / overlap
# ---------------------------------------------------------------------

def test_create_and_query_ipv4_pool(engine):
    engine.create_pool("10.0.0.0/24")
    pools = engine.list_pools()
    assert len(pools) == 1
    assert pools[0]["cidr"] == "10.0.0.0/24"
    assert pools[0]["version"] == 4
    assert pools[0]["total"] == 256


def test_create_and_query_ipv6_pool(engine):
    engine.create_pool("2001:db8::/64")
    info = engine.describe_pool("2001:db8::/64")
    assert info["version"] == 6
    assert info["total"] == 2**64


def test_overlapping_ipv4_pools_rejected(engine):
    engine.create_pool("10.0.0.0/24")
    with pytest.raises(PoolOverlapError):
        engine.create_pool("10.0.0.128/25")
    with pytest.raises(PoolOverlapError):
        engine.create_pool("10.0.0.0/16")


def test_overlapping_ipv6_pools_rejected(engine):
    engine.create_pool("2001:db8::/64")
    with pytest.raises(PoolOverlapError):
        engine.create_pool("2001:db8::/80")


def test_adjacent_and_cross_family_pools_allowed(engine):
    engine.create_pool("10.0.0.0/24")
    engine.create_pool("10.0.1.0/24")
    engine.create_pool("2001:db8::/64")
    assert len(engine.list_pools()) == 3


def test_delete_pool(engine):
    engine.create_pool("10.0.0.0/24")
    engine.delete_pool("10.0.0.0/24")
    assert engine.list_pools() == []
    with pytest.raises(PoolNotFoundError):
        engine.delete_pool("10.0.0.0/24")


def test_delete_pool_with_active_lease_fails(engine):
    engine.create_pool("10.0.0.0/24")
    engine.allocate("10.0.0.0/24")
    with pytest.raises(ActiveLeaseError):
        engine.delete_pool("10.0.0.0/24")


# ---------------------------------------------------------------------
# Automatic allocation
# ---------------------------------------------------------------------

def test_allocate_skips_network_and_broadcast_ipv4(engine):
    engine.create_pool("10.0.0.0/30")
    first = engine.allocate("10.0.0.0/30")
    second = engine.allocate("10.0.0.0/30")
    assert first.ips == ["10.0.0.1"]
    assert second.ips == ["10.0.0.2"]
    # network (10.0.0.0) and broadcast (10.0.0.3) must never be handed out
    with pytest.raises(AddressExhaustedError):
        engine.allocate("10.0.0.0/30")


def test_allocate_skips_subnet_router_anycast_ipv6(engine):
    engine.create_pool("2001:db8::/126")
    lease = engine.allocate("2001:db8::/126")
    assert lease.ips == ["2001:db8::1"]


def test_reserved_addresses_never_allocated(engine):
    engine.create_pool("10.0.0.0/29", reserved=["10.0.0.1", "10.0.0.2"])
    lease = engine.allocate("10.0.0.0/29")
    assert lease.ips == ["10.0.0.3"]


def test_reserved_range_not_allocated(engine):
    engine.create_pool("10.0.0.0/29", reserved=["10.0.0.0/30"])
    lease = engine.allocate("10.0.0.0/29")
    assert lease.ips == ["10.0.0.4"]


def test_address_exhaustion_ipv4(engine):
    engine.create_pool("10.0.0.0/30")
    engine.allocate("10.0.0.0/30")
    engine.allocate("10.0.0.0/30")
    with pytest.raises(AddressExhaustedError):
        engine.allocate("10.0.0.0/30")


def test_address_exhaustion_ipv6(engine):
    engine.create_pool("2001:db8::/127")  # 2 addresses, none reserved at /127
    engine.allocate("2001:db8::/127")
    engine.allocate("2001:db8::/127")
    with pytest.raises(AddressExhaustedError):
        engine.allocate("2001:db8::/127")


# ---------------------------------------------------------------------
# Specific address requests
# ---------------------------------------------------------------------

def test_allocate_specific_ip(engine):
    engine.create_pool("10.0.0.0/24")
    lease = engine.allocate_specific("10.0.0.0/24", "10.0.0.42")
    assert lease.ips == ["10.0.0.42"]


def test_allocate_specific_ipv6(engine):
    engine.create_pool("2001:db8::/64")
    lease = engine.allocate_specific("2001:db8::/64", "2001:db8::dead:beef")
    assert lease.ips == ["2001:db8::dead:beef"]


def test_specific_network_and_broadcast_rejected(engine):
    engine.create_pool("10.0.0.0/24")
    with pytest.raises(AddressUnavailableError):
        engine.allocate_specific("10.0.0.0/24", "10.0.0.0")
    with pytest.raises(AddressUnavailableError):
        engine.allocate_specific("10.0.0.0/24", "10.0.0.255")


def test_specific_reserved_rejected(engine):
    engine.create_pool("10.0.0.0/24", reserved=["10.0.0.10"])
    with pytest.raises(AddressUnavailableError):
        engine.allocate_specific("10.0.0.0/24", "10.0.0.10")


def test_specific_already_allocated_rejected(engine):
    engine.create_pool("10.0.0.0/24")
    engine.allocate_specific("10.0.0.0/24", "10.0.0.5")
    with pytest.raises(AddressUnavailableError):
        engine.allocate_specific("10.0.0.0/24", "10.0.0.5")


def test_specific_outside_pool_rejected(engine):
    engine.create_pool("10.0.0.0/24")
    with pytest.raises(AddressUnavailableError):
        engine.allocate_specific("10.0.0.0/24", "10.0.1.1")


# ---------------------------------------------------------------------
# Contiguous block allocation
# ---------------------------------------------------------------------

def test_contiguous_block_allocation(engine):
    engine.create_pool("10.0.0.0/24")
    lease = engine.allocate("10.0.0.0/24", count=4)
    assert lease.ips == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]


def test_contiguous_block_fails_when_fragmented(engine):
    engine.create_pool("10.0.0.0/29")  # usable: .1 - .6
    engine.allocate_specific("10.0.0.0/29", "10.0.0.3")
    # free: .1 .2 | .4 .5 .6 -> largest contiguous run is 3
    with pytest.raises(AddressExhaustedError):
        engine.allocate("10.0.0.0/29", count=4)
    # a block of 3 still works and is genuinely contiguous
    lease = engine.allocate("10.0.0.0/29", count=3)
    assert lease.ips == ["10.0.0.4", "10.0.0.5", "10.0.0.6"]


def test_contiguous_block_ipv6(engine):
    engine.create_pool("2001:db8::/125")
    lease = engine.allocate("2001:db8::/125", count=3)
    assert lease.ips == ["2001:db8::1", "2001:db8::2", "2001:db8::3"]


# ---------------------------------------------------------------------
# Subnets
# ---------------------------------------------------------------------

def test_subnet_split_and_parent_skips_subnet(engine):
    engine.create_pool("10.0.0.0/24")
    sub = engine.create_subnet("10.0.0.0/24", 26)
    assert sub == "10.0.0.0/26"
    # parent pool must not hand out addresses inside the child subnet
    lease = engine.allocate("10.0.0.0/24")
    assert lease.ips == ["10.0.0.64"]
    # allocate from the subnet itself
    sub_lease = engine.allocate(sub)
    assert sub_lease.ips == ["10.0.0.1"]


def test_subnet_release(engine):
    engine.create_pool("10.0.0.0/24")
    sub = engine.create_subnet("10.0.0.0/24", 26)
    engine.release_subnet(sub)
    lease = engine.allocate("10.0.0.0/24")
    assert lease.ips == ["10.0.0.1"]  # range usable again


def test_subnet_release_blocked_by_active_lease(engine):
    engine.create_pool("10.0.0.0/24")
    sub = engine.create_subnet("10.0.0.0/24", 26)
    engine.allocate(sub)
    with pytest.raises(ActiveLeaseError):
        engine.release_subnet(sub)


def test_subnet_not_carved_over_allocated_addresses(engine):
    engine.create_pool("10.0.0.0/24")
    engine.allocate_specific("10.0.0.0/24", "10.0.0.1")
    sub = engine.create_subnet("10.0.0.0/24", 26)
    assert sub == "10.0.0.64/26"  # first /26 skipped, it contains 10.0.0.1


def test_subnet_exhaustion(engine):
    engine.create_pool("10.0.0.0/24")
    for _ in range(4):
        engine.create_subnet("10.0.0.0/24", 26)
    with pytest.raises(AddressExhaustedError):
        engine.create_subnet("10.0.0.0/24", 26)


# ---------------------------------------------------------------------
# Leases and expiry (injectable clock)
# ---------------------------------------------------------------------

def test_lease_identity_and_timestamps(engine, clock):
    engine.create_pool("10.0.0.0/24")
    lease = engine.allocate("10.0.0.0/24", ttl=60)
    assert lease.lease_id == "lease-000001"
    assert lease.created_at == clock.now
    assert lease.expires_at == clock.now + 60
    lease2 = engine.allocate("10.0.0.0/24")
    assert lease2.lease_id != lease.lease_id
    assert lease2.expires_at is None


def test_expired_lease_address_reclaimed(engine, clock):
    engine.create_pool("10.0.0.0/30")
    lease = engine.allocate("10.0.0.0/30", ttl=60)
    engine.allocate("10.0.0.0/30")
    # pool exhausted while lease is alive
    with pytest.raises(AddressExhaustedError):
        engine.allocate("10.0.0.0/30")
    clock.advance(61)
    # expired address deterministically returns to the allocatable state
    renewed = engine.allocate("10.0.0.0/30")
    assert renewed.ips == lease.ips
    with pytest.raises(LeaseNotFoundError):
        engine.get_lease(lease.lease_id)


def test_unexpired_lease_not_reclaimed(engine, clock):
    engine.create_pool("10.0.0.0/30")
    engine.allocate("10.0.0.0/30", ttl=600)
    engine.allocate("10.0.0.0/30")
    clock.advance(59)
    with pytest.raises(AddressExhaustedError):
        engine.allocate("10.0.0.0/30")


def test_release_lease_frees_address(engine):
    engine.create_pool("10.0.0.0/30")
    lease = engine.allocate("10.0.0.0/30")
    engine.allocate("10.0.0.0/30")
    engine.release_lease(lease.lease_id)
    again = engine.allocate("10.0.0.0/30")
    assert again.ips == lease.ips


# ---------------------------------------------------------------------
# Persistence / restart recovery
# ---------------------------------------------------------------------

def test_state_persisted_and_restored(tmp_path, clock):
    path = str(tmp_path / "ipam-state.json")
    eng = IPAMEngine(state_path=path, clock=clock)
    eng.create_pool("10.0.0.0/24", reserved=["10.0.0.10"])
    eng.create_pool("2001:db8::/64")
    sub = eng.create_subnet("10.0.0.0/24", 26)
    assert sub == "10.0.0.64/26"  # first /26 skipped: holds reserved 10.0.0.10
    lease1 = eng.allocate("10.0.0.0/24", ttl=3600)
    lease2 = eng.allocate_specific("2001:db8::/64", "2001:db8::5")
    lease3 = eng.allocate(sub, count=2)

    # simulate restart: brand-new engine from the same file
    restored = IPAMEngine.load(path, clock=clock)

    assert [p["cidr"] for p in restored.list_pools()] == [
        p["cidr"] for p in eng.list_pools()
    ]
    restored_leases = {l.lease_id: l for l in restored.list_leases()}
    assert set(restored_leases) == {lease1.lease_id, lease2.lease_id, lease3.lease_id}
    assert restored_leases[lease1.lease_id].ips == lease1.ips
    assert restored_leases[lease1.lease_id].expires_at == lease1.expires_at
    assert restored_leases[lease2.lease_id].ips == lease2.ips

    # occupancy identical: allocated addresses stay allocated after restart
    with pytest.raises(AddressUnavailableError):
        restored.allocate_specific("10.0.0.0/24", lease1.ips[0])
    with pytest.raises(AddressUnavailableError):
        restored.allocate_specific("2001:db8::/64", "2001:db8::5")
    # reserved address still reserved after restart
    with pytest.raises(AddressUnavailableError):
        restored.allocate_specific("10.0.0.0/24", "10.0.0.10")
    # next allocation continues where the old engine left off
    assert restored.allocate("10.0.0.0/24").ips == ["10.0.0.2"]


def test_state_file_is_valid_json(tmp_path, clock):
    path = str(tmp_path / "state.json")
    eng = IPAMEngine(state_path=path, clock=clock)
    eng.create_pool("10.0.0.0/24")
    eng.allocate("10.0.0.0/24")
    with open(path) as fh:
        state = json.load(fh)
    assert state["version"] == 1
    assert state["pools"][0]["cidr"] == "10.0.0.0/24"
    assert len(state["leases"]) == 1


def test_expiry_reclaim_survives_restart(tmp_path, clock):
    path = str(tmp_path / "state.json")
    eng = IPAMEngine(state_path=path, clock=clock)
    eng.create_pool("10.0.0.0/30")
    lease = eng.allocate("10.0.0.0/30", ttl=60)
    eng.allocate("10.0.0.0/30")
    clock.advance(61)
    restored = IPAMEngine.load(path, clock=clock)
    renewed = restored.allocate("10.0.0.0/30")
    assert renewed.ips == lease.ips


def test_lease_ids_unique_across_restart(tmp_path, clock):
    path = str(tmp_path / "state.json")
    eng = IPAMEngine(state_path=path, clock=clock)
    eng.create_pool("10.0.0.0/24")
    l1 = eng.allocate("10.0.0.0/24")
    restored = IPAMEngine.load(path, clock=clock)
    l2 = restored.allocate("10.0.0.0/24")
    assert l1.lease_id != l2.lease_id


def test_load_missing_state_file_fails(tmp_path):
    with pytest.raises(IPAMError):
        IPAMEngine.load(str(tmp_path / "nope.json"))
