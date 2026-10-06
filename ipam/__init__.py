"""本地 IPAM（IP 地址管理）引擎。"""

from .clock import FakeClock, SystemClock
from .engine import (
    ActiveLeasesError,
    AddressExhaustedError,
    AddressUnavailableError,
    IPAMEngine,
    IPAMError,
    LeaseNotFoundError,
    NoContiguousBlockError,
    PoolNotFoundError,
    PoolOverlapError,
    SubnetNotFoundError,
)
from .models import Lease, Pool, Subnet

__all__ = [
    "IPAMEngine",
    "IPAMError",
    "PoolOverlapError",
    "PoolNotFoundError",
    "SubnetNotFoundError",
    "LeaseNotFoundError",
    "AddressExhaustedError",
    "NoContiguousBlockError",
    "AddressUnavailableError",
    "ActiveLeasesError",
    "Lease",
    "Pool",
    "Subnet",
    "SystemClock",
    "FakeClock",
]
