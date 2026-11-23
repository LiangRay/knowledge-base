# 拉一条专线到 AWS：Direct Connect 专用连接 + 机房交叉连接全流程（讲解版）

> 来源：一次真实的客户项目复盘。客户通过当地电信运营商，在欧洲某 DX 接入机房拉 1G 专用连接。
> 文中所有 Connection ID、Circuit ID、工单号、端口号都已经**替换成示例值**，不对应任何真实资源。

---

## 0. 一句话结论

**拉 DX 专线，本质上是在 AWS 的 DX 接入机房里做一根"跳线"：把运营商（或你自己）的端口，接到 AWS 在 Meet-Me Room 里的端口上。**
AWS 负责出 LOA-CFA（告诉机房"这个口是我的，允许这个人来接"），机房负责执行交叉连接，运营商负责把线从机房拉到你的数据中心。

打个比方：
- **AWS 端口** = AWS 在机房公共配线间装好的一个"插座"
- **运营商端口** = 运营商在同一个配线间的另一个"插座"
- **LOA-CFA** = AWS 签发的授权书，允许机房把线插到它的插座上
- **交叉连接（Cross-Connect）** = 机房工程师用一根跳线把两个插座连起来

---

## 1. 角色分工

| 角色 | 负责的事 | 交付物 |
|---|---|---|
| **客户（AWS 账号持有人）** | 在 DX 控制台申请连接、下载 LOA，配路由器、VIF、BGP | Connection ID、LOA-CFA |
| **AWS** | 分配 DX 端口，出具 LOA-CFA，维护 MMR 到 DX 路由器那一段 | LOA-CFA（签发方是 AWS 在当地的主体） |
| **运营商 / APN 合作伙伴** | 把线从客户机房拉到 DX 机房，在机房里向机房方下交叉连接单 | 线路、交叉连接工单 |
| **机房方（Equinix / Digital Realty 等）** | 凭 LOA 在 MMR 里做跳线 | Circuit ID（跳线编号） |

> 注意：LOA 上写的签发方不是 "Amazon Web Services"，而是 AWS 在当地登记的法律主体。比如德国机房里常见的是 **A100 Row GmbH**。所以在机房工单的 B 端看到 "A100 ROW" 是正常的，那就是 AWS。

---

## 2. 物理链路长什么样

```
客户机房路由器
   │  运营商线路（城域/长途）
   ▼
运营商在 DX 机房的空间 (CSR, Customer Suite/Space Room)
   │  机房预布的主干光缆（早就布好了）
   ▼
MMR：运营商配线架端口  ═══ 交叉连接跳线（本次机房施工）═══►  MMR：AWS 配线架端口（LOA 上的分界点）
                                                                │  AWS 预布线
                                                                ▼
                                                          AWS DX 路由器
```

一共四段，坏一段都通不了：

| 段 | 谁负责 | 出问题找谁 |
|---|---|---|
| ① 客户机房 → 运营商 CSR | 运营商 | 运营商 |
| ② 运营商 CSR → MMR 运营商端口 | 运营商 / 机房结构化布线 | 运营商 |
| ③ MMR 运营商端口 → MMR AWS 端口（跳线） | 机房方 | 机房方（报 Circuit ID） |
| ④ MMR AWS 端口 → DX 路由器 | AWS | AWS Support case |

---

## 3. 全流程（按时间顺序）

### Step 1：选接入点和带宽
- 在 [DX Locations](https://aws.amazon.com/directconnect/locations/) 里选一个离客户或运营商近的机房，确认它关联的 Region。
- 专用连接的带宽可选 1G / 10G / 100G / 400G。如果带宽小于 1G，或者不想自己跟机房打交道，可以改走 **Hosted Connection**（由 APN 合作伙伴代办），那就没有下面的 LOA 流程。

### Step 2：在 DX 控制台创建连接
控制台 → Direct Connect → Connections → Create connection，选好 Location、端口速率、（可选）运营商名称。
创建后状态是 `requested`。AWS 分配好端口后会变成 `pending`，这时就可以下载 LOA 了，通常在 72 小时内。

```bash
aws directconnect describe-connections --region <region>
aws directconnect describe-loa --connection-id <dxcon-xxxxxxxx> --region <region> \
  --query loaContent --output text | base64 -d > loa.pdf
```

### Step 3：读懂 LOA-CFA
示例（已脱敏）：

| 字段 | 示例值 | 含义 |
|---|---|---|
| Issue Date | 2026-01-01 | 签发日期，**有效期 90 天** |
| Issued By | A100 Row GmbH | AWS 在当地的主体 |
| Requested By | 客户名 (via 运营商) | 运营商代客户下单时这样写 |
| Issued To | 机房名 + 城市 | 交给机房方执行 |
| Facility - Meet Me Room | 机房 - MMR 房间名 | 跳线在哪个房间做 |
| Customer Demarcation / Z-Side | Rack / Patch Panel / Strands | **AWS 的端口位置（分界点）**，跳线就接到这里 |
| Connection ID | dxcon-xxxxxxxx | 对应 DX 控制台里的连接 |
| Optic and Connector | 1000BASE-LX SMF / LC | 对端必须用的光模块和接头 |

### Step 4：把 LOA 交给运营商，由运营商向机房下交叉连接单
运营商要在单子上填 **A 端（自己的端口）** 和 **B 端（照抄 LOA 上的 AWS 端口）**。
机房施工完会给一个 **Circuit ID**，以后报障、查进度都用它。

### Step 5：核对机房工单和 LOA（最关键的一步）
机房工单示例（已脱敏）：

| 字段 | 示例值 |
|---|---|
| A-Side Customer | 运营商名 |
| B-Side Customer | A100 ROW (DE) → 就是 AWS |
| Circuit ID | XX000000 |
| A-side (CSR) | `IDF-xx.Tray xx.Kassette xx.X01` |
| A-side (MMR) | `SITE.CPR.R02.ODF 10.U05.X12` |
| B-side (MMR) | `SITE.CPR.R02.ODF 20.U30.X08` |

**逐项比对 B-side (MMR) 和 LOA 的 Z-Side：**

| 核对项 | LOA | 工单 B 端 | 要求 |
|---|---|---|---|
| 机房 / MMR 房间 | 机房 - CPR | SITE.CPR | 一致（注意机房品牌可能已经被并购改名，比如 Interxion → Digital Realty） |
| 排 | R02 | R02 | 一致 |
| 配线架 | ODF20 | ODF 20 | 一致 |
| 面板 U 位 | U30 | U30 | 一致 |
| 端口 | Strands: 08 | X08 | 一致 |
| B 端主体 | Issued By: A100 Row GmbH | A100 ROW | 都是 AWS |

**端口编号怎么拆**（以 `SITE.CPR.R02.ODF 10.U05.X12` 为例）：

| 段 | 含义 |
|---|---|
| SITE | 机房代码 |
| CPR | MMR 房间名 |
| R02 | 第 02 排机架 |
| ODF 10 | 第 10 号光纤配线架（Optical Distribution Frame） |
| U05 | 配线架上第 5U 的面板 |
| X12 | 面板上第 12 号端口 |

A 端 MMR 口（X12）是运营商的"插座"，B 端 MMR 口（X08）是 AWS 的"插座"，Circuit ID 对应的就是连接两者的那根跳线。
**两个配线架如果在同一个 MMR、同一排，说明只是一根很短的跳线。**

### Step 6：配置客户侧路由器
- 光模块必须和 LOA 上写的一致：1G 用 1000BASE-LX，10G 用 10GBASE-LR，100G 用 100GBASE-LR4，**全部是单模光纤（SMF）**，不能用 SX 多模。
- 端口速率固定，并开启 802.1Q VLAN。
- 1G 端口的自协商：开还是关要和 AWS 端一致。链路起不来时，两种都试一下。
- 运营商一路上的设备（如果是波分或透传）也要支持对应速率和模块。

### Step 7：确认物理链路起来了
```bash
aws directconnect describe-connections --connection-id <dxcon-xxxxxxxx> --region <region> \
  --query 'connections[0].[connectionState,location,awsDeviceV2]'
```
- `connectionState` 从 `pending` 变成 **`available`**
- CloudWatch（命名空间 `AWS/DX`）：
  - `ConnectionState` = 1
  - `ConnectionLightLevelRx` / `Tx` 在正常范围内（1000BASE-LX 大约 -14.4 ~ 0.5 dBm）
  - `ConnectionErrorCount` 不持续增长

### Step 8：创建 VIF、配置 BGP
- **Private VIF**：接 VGW 或 Direct Connect Gateway，用来访问 VPC
- **Transit VIF**：接 DXGW + Transit Gateway，适合多 VPC、多 Region
- **Public VIF**：访问 AWS 公共服务的公网地址段
- 每个 VIF 需要：VLAN ID、BGP ASN、BGP peer IP（可以让 AWS 自动分配）、MD5 key
- 客户路由器上配好 VLAN 子接口和 BGP 后，确认 BGP 状态是 `up`，并且路由已经收到和发出。

### Step 9：高可用（生产环境必做）
- 单条专线没有 SLA 保障。按 [DX Resiliency Toolkit](https://docs.aws.amazon.com/directconnect/latest/UserGuide/resilency_toolkit.html) 的建议：
  - **High**：两个不同的 DX Location，各拉一条
  - **Maximum**：两个 Location，每个 Location 两条，分别落在不同的 AWS 设备上
- 最少也要配一条 **Site-to-Site VPN** 做备份。
- 冗余线路要用 `awsDeviceV2` 确认落在不同的 AWS 设备上。

---

## 4. 排障速查

| 现象 | 最可能的原因 | 怎么处理 |
|---|---|---|
| 一直 `pending`，下不了 LOA | AWS 还没分配端口 | 等 72 小时；超时就开 Support case |
| 跳线做完了，状态还是 down，Rx 没光 | **收发反了**（最常见），或者跳错了口 | 让机房按 Circuit ID 对调收发（roll the fiber），并重新核对 B 端端口和 LOA |
| Rx 有光但很弱 | 光纤脏了、弯折、模块不匹配 | 清洁接头、测光衰；确认是单模和对应的模块类型 |
| 有光但端口 down | 速率或自协商不一致，模块类型错误 | 固定速率，试开/关自协商；检查是不是 LX/LR |
| 物理 up，但 BGP 不起 | VLAN、IP、ASN、MD5 填错 | 对照 VIF 配置逐项核对；在 VLAN 子接口上 ping 对端 peer IP |
| LOA 过期了 | 超过 90 天还没施工 | 控制台重新下载 LOA，交给运营商重新下单 |

**分段定位的思路**：在 MMR 运营商端口那里断开测光。运营商方向有光，说明问题在跳线或 AWS 侧；运营商方向没光，就找运营商。AWS 那一段（④）由客户开 AWS Support case 来查。

---

## 5. 代价和适用边界

| 维度 | 说明 |
|---|---|
| 周期 | 现场施工本身只要几天。但运营商拉线、机房排期加起来**通常要几周到几个月**，要尽早启动 |
| 费用 | AWS 端口小时费 + 数据传出费（DX 费率比公网便宜）+ 机房交叉连接费（一般是一次性加按月）+ 运营商线路费 |
| 不适合 | 临时测试、带宽小于 1G、没有运营商资源的情况 → 改用 Hosted Connection 或 Site-to-Site VPN |
| 适合 | 稳定的大带宽、需要低抖动、有合规要求不走公网的场景 |

---

## 6. 施工前检查清单

- [ ] DX Location 和 Region 选对了
- [ ] 连接状态是 `pending`，LOA 已经下载，并且在 90 天有效期内
- [ ] LOA 已经交给运营商，运营商已向机房下单
- [ ] 机房工单 B 端（MMR）和 LOA 的 Z-Side：房间、排、ODF、U 位、端口**逐项一致**
- [ ] 客户和运营商一路的光模块、接头和 LOA 一致（单模、LC、速率）
- [ ] 已经记下机房 Circuit ID，用来报障
- [ ] 路由器 VLAN 和 BGP 参数已经准备好
- [ ] 冗余方案（第二条 DX 或 VPN）已经规划好
