"""IPAM 引擎自动化测试：全程离线，时间相关用例使用 FakeClock。"""

import ipaddress

import pytest

from ipam import (
    ActiveLeasesError,
    AddressExhaustedError,
    AddressUnavailableError,
    FakeClock,
    IPAMEngine,
    IPAMError,
    LeaseNotFoundError,
    NoContiguousBlockError,
    PoolOverlapError,
    PoolNotFoundError,
)


@pytest.fixture
def clock():
    return FakeClock(start=1_700_000_000.0)


@pytest.fixture
def engine(clock):
    return IPAMEngine(clock=clock)


# --------------------------------------------------------------------- #
# 地址池管理
# --------------------------------------------------------------------- #

class TestPoolManagement:
    def test_create_ipv4_pool(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        assert engine.get_pool(pid).cidr == "10.0.0.0/24"

    def test_create_ipv6_pool(self, engine):
        pid = engine.create_pool("2001:db8::/64")
        assert engine.get_pool(pid).cidr == "2001:db8::/64"

    def test_overlapping_ipv4_pools_rejected(self, engine):
        engine.create_pool("10.0.0.0/24")
        with pytest.raises(PoolOverlapError):
            engine.create_pool("10.0.0.128/25")
        with pytest.raises(PoolOverlapError):
            engine.create_pool("10.0.0.0/16")
        with pytest.raises(PoolOverlapError):
            engine.create_pool("10.0.0.0/24")

    def test_overlapping_ipv6_pools_rejected(self, engine):
        engine.create_pool("2001:db8::/64")
        with pytest.raises(PoolOverlapError):
            engine.create_pool("2001:db8::/80")
        with pytest.raises(PoolOverlapError):
            engine.create_pool("2001:db8::/48")

    def test_cross_family_pools_allowed(self, engine):
        engine.create_pool("10.0.0.0/24")
        engine.create_pool("2001:db8::/64")  # 不同地址族，不视为重叠
        assert len(engine.list_pools()) == 2

    def test_adjacent_pools_allowed(self, engine):
        engine.create_pool("10.0.0.0/25")
        engine.create_pool("10.0.0.128/25")
        assert len(engine.list_pools()) == 2

    def test_delete_pool(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        engine.delete_pool(pid)
        with pytest.raises(PoolNotFoundError):
            engine.get_pool(pid)

    def test_delete_pool_with_active_lease_rejected(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        engine.allocate(pid)
        with pytest.raises(ActiveLeasesError):
            engine.delete_pool(pid)

    def test_reserved_address_must_be_in_pool(self, engine):
        with pytest.raises(IPAMError):
            engine.create_pool("10.0.0.0/24", reserved=["192.168.0.1"])


# --------------------------------------------------------------------- #
# 单地址分配
# --------------------------------------------------------------------- #

class TestSingleAllocation:
    def test_ipv4_skips_network_and_broadcast(self, engine):
        pid = engine.create_pool("10.0.0.0/30")  # 仅 .1 和 .2 可用
        first = engine.allocate(pid)
        second = engine.allocate(pid)
        assert first.ip == "10.0.0.1"
        assert second.ip == "10.0.0.2"
        with pytest.raises(AddressExhaustedError):
            engine.allocate(pid)

    def test_ipv6_skips_network_address(self, engine):
        pid = engine.create_pool("2001:db8::/126")
        lease = engine.allocate(pid)
        assert lease.ip == "2001:db8::1"

    def test_reserved_addresses_never_allocated(self, engine):
        pid = engine.create_pool("10.0.0.0/29", reserved=["10.0.0.1", "10.0.0.2"])
        lease = engine.allocate(pid)
        assert lease.ip == "10.0.0.3"
        with pytest.raises(AddressUnavailableError):
            engine.allocate_specific(pid, "10.0.0.1")

    def test_specific_address(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        lease = engine.allocate_specific(pid, "10.0.0.42")
        assert lease.ip == "10.0.0.42"

    def test_specific_address_conflict(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        engine.allocate_specific(pid, "10.0.0.42")
        with pytest.raises(AddressUnavailableError):
            engine.allocate_specific(pid, "10.0.0.42")

    def test_specific_network_and_broadcast_rejected(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        with pytest.raises(AddressUnavailableError):
            engine.allocate_specific(pid, "10.0.0.0")
        with pytest.raises(AddressUnavailableError):
            engine.allocate_specific(pid, "10.0.0.255")

    def test_specific_out_of_pool_rejected(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        with pytest.raises(AddressUnavailableError):
            engine.allocate_specific(pid, "10.0.1.5")

    def test_exhaustion_ipv4(self, engine):
        pid = engine.create_pool("192.168.1.0/30")
        engine.allocate(pid)
        engine.allocate(pid)
        with pytest.raises(AddressExhaustedError):
            engine.allocate(pid)

    def test_release_makes_address_reusable(self, engine):
        pid = engine.create_pool("10.0.0.0/30")
        lease = engine.allocate(pid)
        engine.allocate(pid)
        engine.release(lease.lease_id)
        again = engine.allocate(pid)
        assert again.ip == lease.ip

    def test_release_unknown_lease(self, engine):
        with pytest.raises(LeaseNotFoundError):
            engine.release("lease-does-not-exist")

    def test_lease_has_unique_id_and_created_at(self, engine, clock):
        pid = engine.create_pool("10.0.0.0/24")
        a = engine.allocate(pid)
        b = engine.allocate(pid)
        assert a.lease_id != b.lease_id
        assert a.created_at == clock.now()


# --------------------------------------------------------------------- #
# 连续地址块
# --------------------------------------------------------------------- #

class TestContiguousBlock:
    def test_block_allocation(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        leases = engine.allocate_block(pid, 4)
        ints = [int(ipaddress.ip_address(l.ip)) for l in leases]
        assert ints == list(range(ints[0], ints[0] + 4))

    def test_block_skips_fragmented_gaps(self, engine):
        pid = engine.create_pool("10.0.0.0/29")  # .1-.6 可用
        engine.allocate_specific(pid, "10.0.0.3")  # 打断连续性
        leases = engine.allocate_block(pid, 3)
        ints = sorted(int(ipaddress.ip_address(l.ip)) for l in leases)
        assert ints == [int(ipaddress.ip_address("10.0.0.4")) + i for i in range(3)]

    def test_block_failure_is_explicit(self, engine):
        pid = engine.create_pool("10.0.0.0/29")  # 6 个可用地址
        engine.allocate_specific(pid, "10.0.0.2")
        engine.allocate_specific(pid, "10.0.0.4")
        engine.allocate_specific(pid, "10.0.0.6")
        # 剩余 .1 .3 .5 均为零散地址，无法凑出连续 2 个
        with pytest.raises(NoContiguousBlockError):
            engine.allocate_block(pid, 2)

    def test_block_larger_than_pool_fails(self, engine):
        pid = engine.create_pool("10.0.0.0/30")
        with pytest.raises(NoContiguousBlockError):
            engine.allocate_block(pid, 3)

    def test_block_ipv6(self, engine):
        pid = engine.create_pool("2001:db8::/126")
        leases = engine.allocate_block(pid, 2)
        assert [l.ip for l in leases] == ["2001:db8::1", "2001:db8::2"]

    def test_block_invalid_size(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        with pytest.raises(IPAMError):
            engine.allocate_block(pid, 0)


# --------------------------------------------------------------------- #
# 租约过期
# --------------------------------------------------------------------- #

class TestLeaseExpiry:
    def test_expired_lease_returns_to_pool(self, engine, clock):
        pid = engine.create_pool("10.0.0.0/30")  # .1 .2
        a = engine.allocate(pid, ttl=60)
        engine.allocate(pid)
        clock.advance(61)
        # 过期租约被回收，.1 重新可分配
        again = engine.allocate(pid)
        assert again.ip == a.ip
        assert again.lease_id != a.lease_id

    def test_unexpired_lease_survives(self, engine, clock):
        pid = engine.create_pool("10.0.0.0/30")
        engine.allocate(pid, ttl=600)
        engine.allocate(pid)
        clock.advance(60)
        with pytest.raises(AddressExhaustedError):
            engine.allocate(pid)

    def test_no_ttl_never_expires(self, engine, clock):
        pid = engine.create_pool("10.0.0.0/30")
        engine.allocate(pid)
        engine.allocate(pid)
        clock.advance(10_000_000)
        with pytest.raises(AddressExhaustedError):
            engine.allocate(pid)

    def test_sweep_returns_expired_ids(self, engine, clock):
        pid = engine.create_pool("10.0.0.0/24")
        a = engine.allocate(pid, ttl=10)
        b = engine.allocate(pid)  # 永不过期
        clock.advance(11)
        swept = engine.sweep_expired()
        assert swept == [a.lease_id]
        assert engine.get_lease(b.lease_id).lease_id == b.lease_id

    def test_expired_lease_get_raises(self, engine, clock):
        pid = engine.create_pool("10.0.0.0/24")
        a = engine.allocate(pid, ttl=10)
        clock.advance(11)
        with pytest.raises(LeaseNotFoundError):
            engine.get_lease(a.lease_id)


# --------------------------------------------------------------------- #
# 子网切分与回收
# --------------------------------------------------------------------- #

class TestSubnets:
    def test_create_subnet(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        sid = engine.create_subnet(pid, 26)
        subnet = [s for s in engine.list_subnets(pid) if s.subnet_id == sid][0]
        assert ipaddress.ip_network(subnet.cidr).prefixlen == 26

    def test_subnets_do_not_overlap(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        s1 = engine.create_subnet(pid, 25)
        s2 = engine.create_subnet(pid, 25)
        nets = [s.network for s in engine.list_subnets(pid)]
        assert not nets[0].overlaps(nets[1])
        assert {s1, s2} == {s.subnet_id for s in engine.list_subnets(pid)}

    def test_pool_allocation_excludes_subnet_space(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        sid = engine.create_subnet(pid, 25)
        subnet_net = engine.list_subnets(pid)[0].network
        lease = engine.allocate(pid)
        assert ipaddress.ip_address(lease.ip) not in subnet_net
        # 子网内分配则落在子网内
        sub_lease = engine.allocate(pid, subnet_id=sid)
        assert ipaddress.ip_address(sub_lease.ip) in subnet_net

    def test_subnet_exhaustion(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        sid = engine.create_subnet(pid, 30)  # 2 个可用地址
        engine.allocate(pid, subnet_id=sid)
        engine.allocate(pid, subnet_id=sid)
        with pytest.raises(AddressExhaustedError):
            engine.allocate(pid, subnet_id=sid)

    def test_delete_subnet_with_active_lease_rejected(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        sid = engine.create_subnet(pid, 26)
        engine.allocate(pid, subnet_id=sid)
        with pytest.raises(ActiveLeasesError):
            engine.delete_subnet(pid, sid)

    def test_delete_subnet_after_release(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        sid = engine.create_subnet(pid, 26)
        lease = engine.allocate(pid, subnet_id=sid)
        engine.release(lease.lease_id)
        engine.delete_subnet(pid, sid)
        assert engine.list_subnets(pid) == []

    def test_subnet_space_reusable_after_delete(self, engine):
        pid = engine.create_pool("10.0.0.0/25")  # .1-.126
        sid = engine.create_subnet(pid, 26)  # 占用 .0-.63
        subnet_net = engine.list_subnets(pid)[0].network
        engine.delete_subnet(pid, sid)
        lease = engine.allocate(pid)
        assert ipaddress.ip_address(lease.ip) in subnet_net  # 空间已回收

    def test_invalid_prefixlen(self, engine):
        pid = engine.create_pool("10.0.0.0/24")
        with pytest.raises(IPAMError):
            engine.create_subnet(pid, 20)
        with pytest.raises(IPAMError):
            engine.create_subnet(pid, 33)


# --------------------------------------------------------------------- #
# 持久化与重启恢复
# --------------------------------------------------------------------- #

class TestPersistence:
    def test_save_and_load_roundtrip(self, engine, clock, tmp_path):
        pid = engine.create_pool("10.0.0.0/24", reserved=["10.0.0.5"])
        v6 = engine.create_pool("2001:db8::/64")
        sid = engine.create_subnet(pid, 26)
        a = engine.allocate(pid)
        b = engine.allocate_specific(pid, "10.0.0.42", ttl=3600)
        c = engine.allocate(pid, subnet_id=sid)
        d = engine.allocate_block(v6, 2)

        state = tmp_path / "ipam-state.json"
        engine.save(state)

        restored = IPAMEngine.load(state, clock=clock)
        assert {p.cidr for p in restored.list_pools()} == {"10.0.0.0/24", "2001:db8::/64"}
        assert restored.get_pool(pid).reserved == {"10.0.0.5"}
        assert [s.cidr for s in restored.list_subnets(pid)] == [
            s.cidr for s in engine.list_subnets(pid)
        ]
        before = sorted(l.ip for l in engine.list_leases())
        after = sorted(l.ip for l in restored.list_leases())
        assert before == after
        # 占用情况一致：已分配地址不能再次分配
        with pytest.raises(AddressUnavailableError):
            restored.allocate_specific(pid, "10.0.0.42")
        # 租约元数据一致
        rb = restored.get_lease(b.lease_id)
        assert rb.expires_at == b.expires_at
        assert rb.created_at == b.created_at
        assert rb.subnet_id == b.subnet_id

    def test_restart_allocation_continues_consistently(self, engine, clock, tmp_path):
        pid = engine.create_pool("10.0.0.0/29")
        engine.allocate(pid)
        engine.allocate(pid)
        state = tmp_path / "state.json"
        engine.save(state)
        restored = IPAMEngine.load(state, clock=clock)
        lease = restored.allocate(pid)
        assert lease.ip == "10.0.0.3"  # 接着重启前的进度分配

    def test_expiry_works_after_restore(self, engine, clock, tmp_path):
        pid = engine.create_pool("10.0.0.0/30")
        a = engine.allocate(pid, ttl=60)
        engine.allocate(pid)
        state = tmp_path / "state.json"
        engine.save(state)
        clock.advance(61)
        restored = IPAMEngine.load(state, clock=clock)
        again = restored.allocate(pid)
        assert again.ip == a.ip

    def test_load_rejects_bad_version(self, tmp_path, clock):
        bad = tmp_path / "bad.json"
        bad.write_text('{"version": 999, "pools": {}, "leases": {}}')
        with pytest.raises(IPAMError):
            IPAMEngine.load(bad, clock=clock)
