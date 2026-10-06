"""本地 IPAM 引擎：地址池、子网、租约、连续块分配与 JSON 持久化。

不依赖 DHCP、数据库、Kubernetes、云网络或任何外部服务。
"""

from __future__ import annotations

import ipaddress
import json
import uuid
from pathlib import Path
from typing import List, Optional, Union

from .clock import SystemClock
from .models import Lease, Pool, Subnet

STATE_VERSION = 1


class IPAMError(Exception):
    """IPAM 引擎基础异常。"""


class PoolOverlapError(IPAMError):
    """地址池与已有地址池重叠。"""


class PoolNotFoundError(IPAMError):
    pass


class SubnetNotFoundError(IPAMError):
    pass


class LeaseNotFoundError(IPAMError):
    pass


class AddressExhaustedError(IPAMError):
    """地址池（或子网）中无可用地址。"""


class NoContiguousBlockError(IPAMError):
    """无法满足连续地址块申请。"""


class AddressUnavailableError(IPAMError):
    """指定地址不可用（已占用、保留、越界或为网络/广播地址）。"""


class ActiveLeasesError(IPAMError):
    """地址池/子网仍存在活跃租约，禁止删除。"""


class IPAMEngine:
    """本地 IP 地址管理引擎。

    参数:
        clock: 可注入时钟，需实现 now() -> float（Unix 秒）。
        state_path: 可选的本地 JSON 状态文件路径；提供时可通过
            save()/load() 或自动持久化保存与恢复全部状态。
    """

    def __init__(self, clock=None, state_path: Optional[Union[str, Path]] = None):
        self._clock = clock if clock is not None else SystemClock()
        self._pools: dict[str, Pool] = {}
        self._leases: dict[str, Lease] = {}
        self.state_path = Path(state_path) if state_path else None

    # ------------------------------------------------------------------ #
    # 地址池管理
    # ------------------------------------------------------------------ #

    def create_pool(self, cidr: str, reserved: Optional[list] = None,
                    pool_id: Optional[str] = None) -> str:
        """创建地址池，严格检测同族地址池重叠。"""
        network = ipaddress.ip_network(cidr, strict=True)
        for pool in self._pools.values():
            existing = pool.network
            if existing.version != network.version:
                continue
            if existing.overlaps(network):
                raise PoolOverlapError(
                    f"pool {cidr} overlaps with existing pool {existing}"
                )
        pid = pool_id or f"pool-{uuid.uuid4().hex[:12]}"
        if pid in self._pools:
            raise IPAMError(f"pool id already exists: {pid}")
        reserved_set = set()
        for item in reserved or []:
            addr = ipaddress.ip_address(item)
            if addr not in network:
                raise IPAMError(f"reserved address {item} not in pool {cidr}")
            reserved_set.add(str(addr))
        self._pools[pid] = Pool(pool_id=pid, cidr=str(network), reserved=reserved_set)
        return pid

    def delete_pool(self, pool_id: str) -> None:
        """删除地址池；存在活跃租约时拒绝删除。"""
        pool = self._get_pool(pool_id)
        self._sweep_expired()
        active = [l for l in self._leases.values() if l.pool_id == pool_id]
        if active:
            raise ActiveLeasesError(
                f"pool {pool_id} has {len(active)} active lease(s)"
            )
        del self._pools[pool_id]

    def get_pool(self, pool_id: str) -> Pool:
        return self._get_pool(pool_id)

    def list_pools(self) -> List[Pool]:
        return list(self._pools.values())

    # ------------------------------------------------------------------ #
    # 子网切分与回收
    # ------------------------------------------------------------------ #

    def create_subnet(self, pool_id: str, prefixlen: int,
                      subnet_id: Optional[str] = None) -> str:
        """从地址池空闲空间中切分一个指定前缀长度的子网。"""
        pool = self._get_pool(pool_id)
        network = pool.network
        if prefixlen < network.prefixlen or prefixlen > network.max_prefixlen:
            raise IPAMError(
                f"invalid prefixlen /{prefixlen} for pool {network}"
            )
        allocated_ints = self._allocated_ints(pool)
        candidate = None
        for sub in network.subnets(new_prefix=prefixlen):
            if any(s.network.overlaps(sub) for s in pool.subnets.values()):
                continue
            first, last = int(sub.network_address), int(sub.broadcast_address)
            if any(first <= a <= last for a in allocated_ints):
                continue
            candidate = sub
            break
        if candidate is None:
            raise AddressExhaustedError(
                f"no free /{prefixlen} subnet available in pool {network}"
            )
        sid = subnet_id or f"subnet-{uuid.uuid4().hex[:12]}"
        if sid in pool.subnets:
            raise IPAMError(f"subnet id already exists: {sid}")
        pool.subnets[sid] = Subnet(subnet_id=sid, pool_id=pool_id, cidr=str(candidate))
        return sid

    def delete_subnet(self, pool_id: str, subnet_id: str) -> None:
        """回收子网；存在活跃租约时拒绝回收。"""
        pool = self._get_pool(pool_id)
        if subnet_id not in pool.subnets:
            raise SubnetNotFoundError(f"subnet not found: {subnet_id}")
        self._sweep_expired()
        active = [l for l in self._leases.values() if l.subnet_id == subnet_id]
        if active:
            raise ActiveLeasesError(
                f"subnet {subnet_id} has {len(active)} active lease(s)"
            )
        del pool.subnets[subnet_id]

    def list_subnets(self, pool_id: str) -> List[Subnet]:
        return list(self._get_pool(pool_id).subnets.values())

    # ------------------------------------------------------------------ #
    # 地址分配
    # ------------------------------------------------------------------ #

    def allocate(self, pool_id: str, subnet_id: Optional[str] = None,
                 ttl: Optional[float] = None) -> Lease:
        """自动分配单个可用 IP（按地址升序取第一个空闲地址）。"""
        pool = self._get_pool(pool_id)
        self._sweep_expired()
        scope = self._scope_network(pool, subnet_id)
        allocated = self._allocated_ints(pool)
        for addr in self._usable_addresses(scope, pool):
            if int(addr) not in allocated:
                return self._new_lease(pool_id, str(addr), ttl, subnet_id)
        raise AddressExhaustedError(f"no free address in scope {scope}")

    def allocate_specific(self, pool_id: str, ip: str,
                          subnet_id: Optional[str] = None,
                          ttl: Optional[float] = None) -> Lease:
        """申请指定 IP；网络/广播/保留/已占用地址一律拒绝。"""
        pool = self._get_pool(pool_id)
        self._sweep_expired()
        scope = self._scope_network(pool, subnet_id)
        addr = ipaddress.ip_address(ip)
        if addr.version != scope.version or addr not in scope:
            raise AddressUnavailableError(f"{ip} is not in scope {scope}")
        if str(addr) in pool.reserved:
            raise AddressUnavailableError(f"{ip} is reserved in pool {pool_id}")
        if not self._is_usable(addr, scope):
            raise AddressUnavailableError(
                f"{ip} is a network/broadcast address of {scope}"
            )
        if int(addr) in self._allocated_ints(pool):
            raise AddressUnavailableError(f"{ip} is already allocated")
        return self._new_lease(pool_id, str(addr), ttl, subnet_id)

    def allocate_block(self, pool_id: str, size: int,
                       subnet_id: Optional[str] = None,
                       ttl: Optional[float] = None) -> List[Lease]:
        """申请 size 个连续地址；无法满足时明确失败，绝不返回零散地址。"""
        if size <= 0:
            raise IPAMError("block size must be positive")
        pool = self._get_pool(pool_id)
        self._sweep_expired()
        scope = self._scope_network(pool, subnet_id)
        allocated = self._allocated_ints(pool)
        run: List[int] = []
        for addr in self._usable_addresses(scope, pool):
            value = int(addr)
            if value in allocated:
                run = []
                continue
            if run and value != run[-1] + 1:
                run = []
            run.append(value)
            if len(run) == size:
                leases = []
                for v in run:
                    addr = ipaddress.ip_address(v)
                    leases.append(self._new_lease(pool_id, str(addr), ttl, subnet_id))
                return leases
        raise NoContiguousBlockError(
            f"no contiguous block of {size} address(es) in scope {scope}"
        )

    def release(self, lease_id: str) -> None:
        """释放租约，地址立即回到可分配状态。"""
        if lease_id not in self._leases:
            raise LeaseNotFoundError(f"lease not found: {lease_id}")
        del self._leases[lease_id]

    def get_lease(self, lease_id: str) -> Lease:
        self._sweep_expired()
        if lease_id not in self._leases:
            raise LeaseNotFoundError(f"lease not found: {lease_id}")
        return self._leases[lease_id]

    def list_leases(self, pool_id: Optional[str] = None,
                    active_only: bool = True) -> List[Lease]:
        if active_only:
            self._sweep_expired()
        leases = list(self._leases.values())
        if pool_id is not None:
            leases = [l for l in leases if l.pool_id == pool_id]
        return leases

    def sweep_expired(self) -> List[str]:
        """按确定规则回收过期租约：expires_at <= now() 即回收。返回回收的租约 ID。"""
        return self._sweep_expired()

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        """将全部状态原子写入本地 JSON 文件。"""
        target = Path(path) if path else self.state_path
        if target is None:
            raise IPAMError("no state path configured")
        payload = {
            "version": STATE_VERSION,
            "pools": {pid: p.to_dict() for pid, p in self._pools.items()},
            "leases": {lid: l.to_dict() for lid, l in self._leases.items()},
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Union[str, Path], clock=None) -> "IPAMEngine":
        """从本地 JSON 文件恢复引擎，地址占用情况与保存时完全一致。"""
        source = Path(path)
        payload = json.loads(source.read_text(encoding="utf-8"))
        if payload.get("version") != STATE_VERSION:
            raise IPAMError(f"unsupported state version: {payload.get('version')}")
        engine = cls(clock=clock, state_path=source)
        engine._pools = {pid: Pool.from_dict(p) for pid, p in payload["pools"].items()}
        engine._leases = {lid: Lease.from_dict(l) for lid, l in payload["leases"].items()}
        return engine

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _get_pool(self, pool_id: str) -> Pool:
        if pool_id not in self._pools:
            raise PoolNotFoundError(f"pool not found: {pool_id}")
        return self._pools[pool_id]

    def _scope_network(self, pool: Pool, subnet_id: Optional[str]):
        if subnet_id is None:
            return pool.network
        if subnet_id not in pool.subnets:
            raise SubnetNotFoundError(f"subnet not found: {subnet_id}")
        return pool.subnets[subnet_id].network

    def _sweep_expired(self) -> List[str]:
        now = self._clock.now()
        expired = [lid for lid, l in self._leases.items() if l.is_expired(now)]
        for lid in expired:
            del self._leases[lid]
        return expired

    def _allocated_ints(self, pool: Pool) -> set:
        """池中已占用地址（含各子网内的租约），以整数表示。"""
        return {
            int(ipaddress.ip_address(l.ip))
            for l in self._leases.values()
            if l.pool_id == pool.pool_id
        }

    def _is_usable(self, addr, network) -> bool:
        """IPv4 排除网络与广播地址；IPv6 排除子网路由器任播地址（网络地址）。"""
        if addr == network.network_address:
            return False
        if network.version == 4 and addr == network.broadcast_address:
            return False
        return True

    def _usable_addresses(self, network, pool: Pool):
        """作用域内可分配地址：排除网络/广播地址与配置保留地址。

        池级分配时同时排除已被子网覆盖的范围。
        """
        if network == pool.network:
            excluded = [s.network for s in pool.subnets.values()]
        else:
            excluded = []
        for addr in network:
            if not self._is_usable(addr, network):
                continue
            if str(addr) in pool.reserved:
                continue
            if any(addr in sub for sub in excluded):
                continue
            yield addr

    def _new_lease(self, pool_id: str, ip: str, ttl: Optional[float],
                   subnet_id: Optional[str]) -> Lease:
        now = self._clock.now()
        lease = Lease(
            lease_id=f"lease-{uuid.uuid4().hex[:12]}",
            ip=ip,
            pool_id=pool_id,
            created_at=now,
            expires_at=(now + ttl) if ttl is not None else None,
            subnet_id=subnet_id,
        )
        self._leases[lease.lease_id] = lease
        return lease
