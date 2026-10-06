"""IPAM 数据模型。所有状态仅存在于内存或本地 JSON 文件中。"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Optional, Union

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


@dataclass
class Lease:
    """一次地址租约。expires_at 为 None 表示永不过期。"""

    lease_id: str
    ip: str
    pool_id: str
    created_at: float
    expires_at: Optional[float] = None
    subnet_id: Optional[str] = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "ip": self.ip,
            "pool_id": self.pool_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "subnet_id": self.subnet_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lease":
        return cls(
            lease_id=data["lease_id"],
            ip=data["ip"],
            pool_id=data["pool_id"],
            created_at=float(data["created_at"]),
            expires_at=data["expires_at"],
            subnet_id=data.get("subnet_id"),
        )


@dataclass
class Subnet:
    """从地址池中切分出的子网。"""

    subnet_id: str
    pool_id: str
    cidr: str

    @property
    def network(self) -> Network:
        return ipaddress.ip_network(self.cidr)

    def to_dict(self) -> dict:
        return {"subnet_id": self.subnet_id, "pool_id": self.pool_id, "cidr": self.cidr}

    @classmethod
    def from_dict(cls, data: dict) -> "Subnet":
        return cls(subnet_id=data["subnet_id"], pool_id=data["pool_id"], cidr=data["cidr"])


@dataclass
class Pool:
    """一个 CIDR 地址池。"""

    pool_id: str
    cidr: str
    reserved: set = field(default_factory=set)  # 配置保留地址（字符串）
    subnets: dict = field(default_factory=dict)  # subnet_id -> Subnet

    @property
    def network(self) -> Network:
        return ipaddress.ip_network(self.cidr)

    def to_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
            "cidr": self.cidr,
            "reserved": sorted(self.reserved),
            "subnets": {sid: s.to_dict() for sid, s in self.subnets.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Pool":
        return cls(
            pool_id=data["pool_id"],
            cidr=data["cidr"],
            reserved=set(data.get("reserved", [])),
            subnets={sid: Subnet.from_dict(s) for sid, s in data.get("subnets", {}).items()},
        )
