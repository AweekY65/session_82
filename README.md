# 本地 IPAM 引擎

一个纯本地的 IP 地址管理（IPAM）引擎，基于 Python 标准库实现。所有地址池、
子网、租约、分配记录和状态只持久化在**内存或本地 JSON 文件**中，不依赖
DHCP 服务、数据库、Kubernetes、云网络或任何外部服务。

## 功能

- IPv4 / IPv6 CIDR 地址池的创建、删除与查询，严格检测同族地址段重叠
- 自动分配单个 IP，或申请指定 IP
- 子网切分与回收，活跃租约保护
- 连续地址块申请，无法满足时明确失败
- 租约管理：唯一 ID、创建时间、可选 TTL，过期后按确定规则回收
- 状态保存 / 恢复，重启后地址占用情况与重启前完全一致

## CIDR 规则

- 地址池以严格 CIDR 表示（如 `10.0.0.0/24`、`2001:db8::/64`），主机位必须为零。
- 同一地址族（IPv4 或 IPv6）内不允许地址池重叠；跨地址族不视为重叠。
- IPv4 池自动排除**网络地址**与**广播地址**；IPv6 池排除**网络地址**
  （子网路由器任播地址），IPv6 无广播地址概念。
- 创建池时可通过 `reserved=[...]` 配置保留地址，保留地址永不参与分配，
  且必须位于池内。
- 子网通过 `create_subnet(pool_id, prefixlen)` 从池的空闲空间切分，
  子网之间互不重叠；池级分配自动跳过已被子网覆盖的范围。

## 地址分配策略

- **自动分配**：`allocate(pool_id)` 按地址升序返回第一个空闲可用地址。
- **指定地址**：`allocate_specific(pool_id, ip)` 申请固定 IP；网络/广播地址、
  保留地址、已占用地址、池外地址一律以 `AddressUnavailableError` 拒绝。
- **连续块**：`allocate_block(pool_id, size)` 返回 `size` 个数值连续的地址；
  若池中只剩零散空隙，抛出 `NoContiguousBlockError`，绝不返回零散地址冒充。
- **子网内分配**：以上接口均接受可选 `subnet_id`，限定在子网范围内分配。
- **释放**：`release(lease_id)` 立即将地址归还到可分配状态。

## 租约与过期

每个租约包含唯一 `lease_id`、`created_at` 和可选 `expires_at`
（由 `ttl` 秒数推导，`ttl=None` 表示永不过期）。

过期判定遵循确定规则：`now() >= expires_at` 即过期。过期租约在每次分配
操作前以及调用 `sweep_expired()` 时被回收，地址重新进入可分配状态。
时间来源为可注入时钟（`SystemClock` / `FakeClock`），测试可精确控制时间。

## 持久化

```python
from ipam import IPAMEngine

engine = IPAMEngine(state_path="ipam-state.json")
pool_id = engine.create_pool("10.0.0.0/24", reserved=["10.0.0.1"])
lease = engine.allocate(pool_id, ttl=3600)
engine.save()                       # 原子写入本地 JSON 文件

restored = IPAMEngine.load("ipam-state.json")   # 重启后恢复，占用情况完全一致
```

状态文件为单个 JSON 文档（含版本号），通过「临时文件 + 原子替换」写入，
避免中途崩溃损坏状态。

## 目录结构

```
ipam/
  clock.py    # 可注入时钟（SystemClock / FakeClock）
  models.py   # Pool / Subnet / Lease 数据模型
  engine.py   # IPAMEngine 核心逻辑
tests/
  test_ipam.py
```

## 运行测试

测试全程离线，不访问任何网络或 GUI：

```bash
python3 -m pytest          # 详细输出
python3 -m pytest -q       # 简洁输出
```

覆盖场景：IPv4/IPv6 地址池、重叠检测、地址耗尽、指定地址、保留地址、
连续块分配与失败、子网切分与回收保护、租约过期（FakeClock 注入时间）、
状态保存与重启恢复。
