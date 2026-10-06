"""Local IPAM (IP Address Management) engine.

Pure standard-library implementation. All state (pools, subnets, leases,
allocations) lives in memory and is optionally persisted to a local JSON
file. No DHCP, database, Kubernetes, cloud or any external service is used.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


class IPAMError(Exception):
    """Base class for all IPAM errors."""


class PoolOverlapError(IPAMError):
    """Raised when a pool/subnet overlaps an existing pool or subnet."""


class PoolNotFoundError(IPAMError):
    """Raised when the referenced pool or subnet does not exist."""


class AddressExhaustedError(IPAMError):
    """Raised when no (contiguous) address space is available."""


class AddressUnavailableError(IPAMError):
    """Raised when a specifically requested address cannot be allocated."""


class ActiveLeaseError(IPAMError):
    """Raised when deleting a range that still has active leases."""


class LeaseNotFoundError(IPAMError):
    """Raised when the referenced lease does not exist."""


def _network_reserved_addresses(network: IPNetwork) -> set:
    """Addresses never handed out: network (and broadcast for IPv4).

    IPv4 /31 (point-to-point, RFC 3021) and /32 have no reserved addresses.
    IPv6 reserves the subnet-router anycast address (first address) unless
    the network is a /127 or /128.
    """
    reserved = set()
    if isinstance(network, ipaddress.IPv4Network):
        if network.prefixlen < 31:
            reserved.add(int(network.network_address))
            reserved.add(int(network.broadcast_address))
    else:
        if network.prefixlen < 127:
            reserved.add(int(network.network_address))
    return reserved


@dataclass
class Lease:
    """A lease over one or more contiguous addresses."""

    lease_id: str
    container: str  # CIDR of the pool or subnet the lease belongs to
    ips: List[str]
    created_at: float
    expires_at: Optional[float] = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "container": self.container,
            "ips": list(self.ips),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lease":
        return cls(
            lease_id=data["lease_id"],
            container=data["container"],
            ips=list(data["ips"]),
            created_at=float(data["created_at"]),
            expires_at=(
                None if data["expires_at"] is None else float(data["expires_at"])
            ),
        )


class _AddressContainer:
    """A network (pool or subnet) that tracks reserved/allocated addresses."""

    def __init__(self, cidr: str, reserved: Optional[List[str]] = None):
        self.network: IPNetwork = ipaddress.ip_network(cidr, strict=True)
        self.reserved: set = _network_reserved_addresses(self.network)
        self.user_reserved: set = set()
        for item in reserved or []:
            if "/" in item:
                sub = ipaddress.ip_network(item, strict=True)
                if sub.version != self.network.version or not sub.subnet_of(
                    self.network
                ):
                    raise IPAMError(
                        f"reserved range {item} is not inside {self.network}"
                    )
                self.user_reserved.update(int(a) for a in sub)
            else:
                addr = ipaddress.ip_address(item)
                if addr not in self.network:
                    raise IPAMError(
                        f"reserved address {item} is not inside {self.network}"
                    )
                self.user_reserved.add(int(addr))
        self.reserved.update(self.user_reserved)
        self.allocated: Dict[int, str] = {}  # address int -> lease_id

    @property
    def cidr(self) -> str:
        return str(self.network)

    def is_usable(self, addr_int: int, extra_blocked: set) -> bool:
        return (
            int(self.network.network_address)
            <= addr_int
            <= int(self.network.broadcast_address)
            and addr_int not in self.reserved
            and addr_int not in self.allocated
            and addr_int not in extra_blocked
        )


class _Pool(_AddressContainer):
    def __init__(self, cidr: str, reserved: Optional[List[str]] = None):
        super().__init__(cidr, reserved)
        self.subnets: Dict[str, "_Subnet"] = {}

    def blocked_by_subnets(self) -> set:
        blocked = set()
        for sub in self.subnets.values():
            blocked.update(
                range(
                    int(sub.network.network_address),
                    int(sub.network.broadcast_address) + 1,
                )
            )
        return blocked


class _Subnet(_AddressContainer):
    pass


class IPAMEngine:
    """Local IPAM engine.

    Parameters
    ----------
    state_path:
        Optional path to a local JSON file. Every mutation is persisted
        atomically; pass the same path to a new engine (or ``load``) to
        restore the exact state after a restart.
    clock:
        Injectable clock returning epoch seconds. Defaults to ``time.time``.
    """

    STATE_VERSION = 1

    def __init__(
        self,
        state_path: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self._clock = clock or time.time
        self._state_path = state_path
        self._pools: Dict[str, _Pool] = {}
        self._leases: Dict[str, Lease] = {}
        self._lease_seq = 0
        if state_path and os.path.exists(state_path):
            self._restore(state_path)

    # ------------------------------------------------------------------
    # Construction / persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls, state_path: str, clock: Optional[Callable[[], float]] = None
    ) -> "IPAMEngine":
        """Restore an engine from a previously persisted state file."""
        if not os.path.exists(state_path):
            raise IPAMError(f"state file not found: {state_path}")
        return cls(state_path=state_path, clock=clock)

    def _now(self) -> float:
        return float(self._clock())

    def _save(self) -> None:
        if not self._state_path:
            return
        state = {
            "version": self.STATE_VERSION,
            "lease_seq": self._lease_seq,
            "pools": [
                {
                    "cidr": pool.cidr,
                    "reserved": sorted(pool.reserved),
                    "subnets": [
                        {"cidr": sub.cidr, "reserved": sorted(sub.reserved)}
                        for sub in pool.subnets.values()
                    ],
                }
                for pool in self._pools.values()
            ],
            "leases": [lease.to_dict() for lease in self._leases.values()],
        }
        directory = os.path.dirname(os.path.abspath(self._state_path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ipam-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _restore(self, state_path: str) -> None:
        with open(state_path) as fh:
            state = json.load(fh)
        if state.get("version") != self.STATE_VERSION:
            raise IPAMError(f"unsupported state version: {state.get('version')}")
        self._lease_seq = int(state.get("lease_seq", 0))
        for pdata in state.get("pools", []):
            pool = _Pool(pdata["cidr"])
            pool.reserved = set(pdata.get("reserved", []))
            for sdata in pdata.get("subnets", []):
                sub = _Subnet(sdata["cidr"])
                sub.reserved = set(sdata.get("reserved", []))
                pool.subnets[sub.cidr] = sub
            self._pools[pool.cidr] = pool
        for ldata in state.get("leases", []):
            lease = Lease.from_dict(ldata)
            self._leases[lease.lease_id] = lease
            container = self._find_container(lease.container)
            for ip in lease.ips:
                container.allocated[int(ipaddress.ip_address(ip))] = lease.lease_id

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _find_container(self, cidr: str) -> _AddressContainer:
        key = str(ipaddress.ip_network(cidr, strict=True))
        if key in self._pools:
            return self._pools[key]
        for pool in self._pools.values():
            if key in pool.subnets:
                return pool.subnets[key]
        raise PoolNotFoundError(f"no pool or subnet for {cidr}")

    def _parent_pool_of(self, cidr: str) -> Optional[_Pool]:
        key = str(ipaddress.ip_network(cidr, strict=True))
        for pool in self._pools.values():
            if key in pool.subnets:
                return pool
        return None

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------

    def create_pool(self, cidr: str, reserved: Optional[List[str]] = None) -> str:
        """Create an address pool. Overlapping pools are rejected."""
        pool = _Pool(cidr, reserved)
        for existing in self._pools.values():
            if pool.network.overlaps(existing.network):
                raise PoolOverlapError(
                    f"pool {pool.cidr} overlaps existing pool {existing.cidr}"
                )
        self._pools[pool.cidr] = pool
        self._save()
        return pool.cidr

    def delete_pool(self, cidr: str) -> None:
        """Delete a pool. Fails while any lease in its range is active."""
        key = str(ipaddress.ip_network(cidr, strict=True))
        if key not in self._pools:
            raise PoolNotFoundError(f"no pool for {cidr}")
        self._reap_expired()
        pool = self._pools[key]
        self._assert_no_active_leases([pool.cidr] + list(pool.subnets))
        del self._pools[key]
        self._save()

    def list_pools(self) -> List[dict]:
        return [self.describe_pool(cidr) for cidr in sorted(self._pools)]

    def describe_pool(self, cidr: str) -> dict:
        pool = self._pools.get(str(ipaddress.ip_network(cidr, strict=True)))
        if pool is None:
            raise PoolNotFoundError(f"no pool for {cidr}")
        self._reap_expired()
        return {
            "cidr": pool.cidr,
            "version": pool.network.version,
            "total": pool.network.num_addresses,
            "allocated": len(pool.allocated),
            "reserved": len(pool.reserved),
            "subnets": sorted(pool.subnets),
        }

    # ------------------------------------------------------------------
    # Subnets
    # ------------------------------------------------------------------

    def create_subnet(
        self, pool_cidr: str, prefixlen: int, reserved: Optional[List[str]] = None
    ) -> str:
        """Carve the lowest available child subnet of ``prefixlen`` from a pool."""
        pool = self._pools.get(str(ipaddress.ip_network(pool_cidr, strict=True)))
        if pool is None:
            raise PoolNotFoundError(f"no pool for {pool_cidr}")
        if not isinstance(prefixlen, int) or not (
            pool.network.prefixlen < prefixlen <= pool.network.max_prefixlen
        ):
            raise IPAMError(
                f"invalid prefixlen {prefixlen} for pool {pool.cidr}"
            )
        blocked = pool.blocked_by_subnets()
        for candidate in pool.network.subnets(new_prefix=prefixlen):
            start = int(candidate.network_address)
            end = int(candidate.broadcast_address)
            if any(
                addr in pool.user_reserved
                or addr in pool.allocated
                or addr in blocked
                for addr in range(start, end + 1)
            ):
                continue
            sub = _Subnet(str(candidate), reserved)
            pool.subnets[sub.cidr] = sub
            self._save()
            return sub.cidr
        raise AddressExhaustedError(
            f"no free /{prefixlen} subnet available in {pool.cidr}"
        )

    def release_subnet(self, cidr: str) -> None:
        """Return a subnet to its parent pool. Fails with active leases."""
        key = str(ipaddress.ip_network(cidr, strict=True))
        parent = self._parent_pool_of(key)
        if parent is None:
            raise PoolNotFoundError(f"no subnet for {cidr}")
        self._reap_expired()
        self._assert_no_active_leases([key])
        del parent.subnets[key]
        self._save()

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------

    def allocate(
        self,
        cidr: str,
        count: int = 1,
        ttl: Optional[float] = None,
    ) -> Lease:
        """Allocate ``count`` contiguous addresses from a pool or subnet.

        Raises ``AddressExhaustedError`` if no contiguous block of the
        requested size exists; scattered addresses are never returned in
        place of a contiguous range.
        """
        if count < 1:
            raise IPAMError("count must be >= 1")
        container = self._find_container(cidr)
        self._reap_expired()
        blocked = (
            container.blocked_by_subnets()
            if isinstance(container, _Pool)
            else set()
        )
        start = int(container.network.network_address)
        end = int(container.network.broadcast_address)
        run: List[int] = []
        for addr in range(start, end + 1):
            if container.is_usable(addr, blocked):
                run.append(addr)
                if len(run) == count:
                    return self._make_lease(container, run, ttl)
            else:
                run = []
        raise AddressExhaustedError(
            f"no contiguous block of {count} address(es) in {container.cidr}"
        )

    def allocate_specific(
        self, cidr: str, ip: str, ttl: Optional[float] = None
    ) -> Lease:
        """Allocate a specific address. Network/broadcast/reserved addresses
        and already-allocated addresses are rejected."""
        container = self._find_container(cidr)
        self._reap_expired()
        addr = ipaddress.ip_address(ip)
        if addr not in container.network:
            raise AddressUnavailableError(
                f"{ip} is not inside {container.cidr}"
            )
        addr_int = int(addr)
        if addr_int in container.reserved:
            raise AddressUnavailableError(
                f"{ip} is reserved (network/broadcast/configured) in {container.cidr}"
            )
        blocked = (
            container.blocked_by_subnets()
            if isinstance(container, _Pool)
            else set()
        )
        if addr_int in blocked:
            raise AddressUnavailableError(
                f"{ip} belongs to child subnet of {container.cidr}"
            )
        if addr_int in container.allocated:
            raise AddressUnavailableError(f"{ip} is already allocated")
        return self._make_lease(container, [addr_int], ttl)

    def release_lease(self, lease_id: str) -> None:
        lease = self._leases.pop(lease_id, None)
        if lease is None:
            raise LeaseNotFoundError(f"no lease {lease_id}")
        container = self._find_container(lease.container)
        for ip in lease.ips:
            container.allocated.pop(int(ipaddress.ip_address(ip)), None)
        self._save()

    def get_lease(self, lease_id: str) -> Lease:
        self._reap_expired()
        try:
            return self._leases[lease_id]
        except KeyError:
            raise LeaseNotFoundError(f"no lease {lease_id}") from None

    def list_leases(self) -> List[Lease]:
        self._reap_expired()
        return sorted(self._leases.values(), key=lambda l: l.lease_id)

    # ------------------------------------------------------------------
    # Lease internals
    # ------------------------------------------------------------------

    def _make_lease(
        self, container: _AddressContainer, addr_ints: List[int], ttl: Optional[float]
    ) -> Lease:
        now = self._now()
        self._lease_seq += 1
        lease = Lease(
            lease_id=f"lease-{self._lease_seq:06d}",
            container=container.cidr,
            ips=[str(ipaddress.ip_address(a)) for a in addr_ints],
            created_at=now,
            expires_at=None if ttl is None else now + float(ttl),
        )
        for addr_int in addr_ints:
            container.allocated[addr_int] = lease.lease_id
        self._leases[lease.lease_id] = lease
        self._save()
        return lease

    def _reap_expired(self) -> None:
        """Deterministically return expired leases to the allocatable state."""
        now = self._now()
        expired = [
            lease
            for lease in self._leases.values()
            if lease.is_expired(now)
        ]
        if not expired:
            return
        for lease in sorted(expired, key=lambda l: l.lease_id):
            container = self._find_container(lease.container)
            for ip in lease.ips:
                container.allocated.pop(int(ipaddress.ip_address(ip)), None)
            del self._leases[lease.lease_id]
        self._save()

    def _assert_no_active_leases(self, containers: List[str]) -> None:
        for lease in self._leases.values():
            if lease.container in containers:
                raise ActiveLeaseError(
                    f"range {lease.container} has active lease {lease.lease_id}"
                )
