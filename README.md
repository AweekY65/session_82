# Local IPAM Engine

一个纯本地运行的 IP 地址管理（IPAM）引擎，仅依赖 Python 标准库
（`ipaddress` / `json`）。所有地址池、子网、租约、分配记录和状态只持久化在
本地文件或内存中，**不依赖** DHCP 服务、数据库、Kubernetes、云网络或任何
外部服务。

## 功能概览

- IPv4 与 IPv6 CIDR 地址池的创建、删除、查询，严格检测重叠地址段
- 自动分配单个 IP，或申请指定 IP
- 连续地址块申请；无法满足连续空间时明确失败，绝不返回零散地址
- 子网切分与回收；存在活跃租约的范围禁止删除/回收
- 租约具有唯一 ID、创建时间和可选过期时间，过期地址按确定规则回收
- 状态保存与恢复：重启后地址占用情况与重启前完全一致

## CIDR 规则

- 地址池/子网必须以标准 CIDR 表示（如 `10.0.0.0/24`、`2001:db8::/64`），
  主机位必须为零（strict 模式）。
- 新地址池与任何现有地址池（同地址族）重叠时抛出 `PoolOverlapError`；
  相邻池、不同地址族的池不受影响。
- 自动保留地址（永不被分配）：
  - IPv4：网络地址与广播地址（`/31`、`/32` 除外，遵循 RFC 3021 点对点语义）。
  - IPv6：子网路由器任播地址（即网络地址，`/127`、`/128` 除外）。
- 创建池时可通过 `reserved=[...]` 配置额外保留地址或保留段（CIDR），
  保留地址同样永不被分配，申请时抛出 `AddressUnavailableError`。

## 地址分配策略

- **自动分配** `allocate(cidr, count=1, ttl=None)`：从最低可用地址开始扫描，
  返回包含 `count` 个**连续**地址的租约；无足够连续空间时抛出
  `AddressExhaustedError`，不会用零散地址冒充连续区间。
- **指定分配** `allocate_specific(cidr, ip, ttl=None)`：申请指定地址；
  网络/广播/保留地址、已分配地址、池外地址均被拒绝
  （`AddressUnavailableError`）。
- **子网** `create_subnet(pool_cidr, prefixlen)`：从池中切出最低的可用
  子网（跳过已分配地址、用户保留地址和已有子网）。子网范围在父池中
  自动排除，可独立分配。`release_subnet(cidr)` 回收子网，存在活跃
  租约时抛出 `ActiveLeaseError`。
- **租约**：每个租约含全局唯一 ID（`lease-000001` 递增，重启后不重复）、
  创建时间和可选过期时间（`ttl` 秒）。每次分配/查询前确定性地回收
  全部过期租约（按租约 ID 升序），过期地址重新进入可分配状态。

## 持久化方式

传入 `state_path` 后，每次变更（建池、分配、释放、过期回收等）都会以
JSON 快照**原子写入**本地文件（临时文件 + `os.replace`）。重启后用同一
路径构造引擎或调用 `IPAMEngine.load(path)` 即可完全恢复地址占用情况：

```python
from ipam import IPAMEngine

engine = IPAMEngine(state_path="ipam-state.json")
engine.create_pool("10.0.0.0/24", reserved=["10.0.0.10"])
lease = engine.allocate("10.0.0.0/24", ttl=3600)

# 进程重启后：
restored = IPAMEngine.load("ipam-state.json")
assert [l.lease_id for l in restored.list_leases()] == [lease.lease_id]
```

## 可注入时钟

时间相关逻辑通过 `clock` 参数注入（默认为 `time.time`），测试中使用
假时钟精确控制租约过期：

```python
engine = IPAMEngine(clock=lambda: fake_now)
```

## 运行测试

测试基于 pytest，直接在终端执行，无需任何外部网络或 GUI：

```bash
python3 -m pytest tests/ -v
```

覆盖场景：IPv4/IPv6 地址池、地址池重叠、地址耗尽、指定地址分配、
连续块分配与碎片化失败、子网切分/回收、租约过期回收、状态持久化与
重启恢复等。
