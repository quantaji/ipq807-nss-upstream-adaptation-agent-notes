# 阶段 1　第五步：devlink 端口和健康报告

第五步给新驱动加三样东西：一台 devlink 设备、每个有网口的口一个 devlink 端口、健康报告。这份文件管 10 件事，决定号是 决定-001 到 决定-010。每一件读到的资料都在 `5-devlink端口和健康报告-资料.md` 里，这份文件只放决定和推理。下面说"资料文件"，指的就是那一份；说"第四步设计第九节"，指的是 `4-多环和日常功能.md` 的第九节。

每个决定依次写：资料归类以后的结果、每一种做得成的做法、演绎、去处和理由、结论和设计说明。去处是"要用户定"的，多写建议和"你的回答"。每一类几家的数，都是程序从资料文件的名单里数出来的。源码的链接指到本地的 Linux 源码（7.3-rc1）、新驱动和 OpenWrt 上游的源码，后面跟着那一行的原句；新驱动是和 6.18.44 的内核一起编译的，这份文件引到的内核函数在两版里都有。

## 总表

| 决定号 | 要定什么 | 去处 | 一句话结论 |
|---|---|---|---|
| 决定-001 | devlink 端口的种类（`flavour`）、口号（`phys.port_number`）和登记用的编号（`port_index`）各填什么 | 自己定 | 五个有网口的口登记成"物理口"，口号和登记用的编号都用 PPE 里这个口的编号（2 到 6）；用户读到的物理口名是 p2 到 p6，和 OpenWrt 上游的镜像一样。 |
| 决定-002 | "属于哪台交换机"的编号（`switch_id`）填不填、填什么 | 要用户定 | 填：五个口填同一个"属于哪台交换机"的编号，内容用 PPE 的寄存器起始地址。它说明这五个口在同一块交换硬件上；驱动不做硬件转发时，内核不拿它改变任何转发。 |
| 决定-003 | 没有网口的口（通 CPU 的口、芯片里有但是板子上没有用的口）登记不登记 devlink 端口 | 要用户定 | 只给五个有网口的口登记 devlink 端口；通 CPU 的口和板子上没有用的口不登记。`devlink port show` 是五行，OpenWrt 上游的镜像上是八行。 |
| 决定-004 | 通道数（`lanes`）、能不能拆（`splittable`）填不填 | 自己定 | 通道数和"能不能拆"都不填，不登记拆口、合口的回调：这块板子的每个口和 PHY 之间是固定的一路，没有可以拆的东西。和 OpenWrt 上游的镜像一样。 |
| 决定-005 | 健康报告设几个、各管哪几种故障、挂在设备上还是挂在端口上 | 要用户定 | 设三个健康报告，都挂在设备上：`edma_hw` 管硬件报错的中断报的九种错（靠恢复的回调再打开）；`edma_ring` 管环上的账对不上的两样（不能恢复）；`edma_rxfill` 管空缓冲区环空了（驱动补上以后自己把状态改回"健康"）。状态里没有认得的位只计数。 |
| 决定-006 | 故障在哪里交给健康报告；同一种故障连着来时怎样算 | 自己定 | 中断处理函数和轮询里只记下故障、排一个工作，在工作里上报。硬件报的错：报了的位在中断处理函数里已经关掉，再打开以前同一种错不会再来。账对不上、空缓冲区环空了：轮询里只记次数和头一次的现场，工作里报一次。 |
| 决定-007 | 报告怎样回到"健康"：恢复的回调、宽限期、自动恢复 | 要用户定 | `edma_hw` 有恢复的回调、自动恢复开着、宽限期 5 秒；恢复的回调先看状态寄存器，还报着的位不打开。`edma_ring` 没有恢复的回调，出过错就一直是"出错"。`edma_rxfill` 没有恢复的回调，驱动补上缓冲以后自己把它改回"健康"。 |
| 决定-008 | 诊断、存现场、测试三个回调各给用户什么 | 要用户定 | 每个报告都给诊断和存现场，不给测试。诊断读的是这一刻的硬件状态和计数；存现场是出错那一刻由内核自动存下的一份。 |
| 决定-009 | devlink 设备的回调表（`struct devlink_ops`）和端口的回调表（`struct devlink_port_ops`）各登记哪几个回调 | 要用户定 | devlink 设备的回调表一个回调都不填，端口不带回调表。其中丢包原因那一组填不填，和第四步设计第九节决定-012（丢在 PPE 里的帧在哪里看）是同一件事，跟着那一个回答走；别的各组管的东西新驱动在阶段 1 都没有。`devlink dev info` 照样有驱动的名字，由内核自己填。 |
| 决定-010 | 镜像里默认带不带 `devlink` 这个命令 | 要用户定 | 这块板子的镜像默认带上 `devlink` 这个命令：健康报告的状态、诊断、存下的现场、手动恢复都只有用它才看得到、做得了。这要改驱动以外的东西（这块板子的默认软件包）。 |

## 决定-001　devlink 端口的种类（`flavour`）、口号（`phys.port_number`）和登记用的编号（`port_index`）各填什么

去处：自己定。

**资料归类以后的结果**

- 内核怎样规定：
  - 端口要在登记网口以前连到网口上，登记以后再连内核会警告：[include/linux/netdevice.h 第 2659 行](../../../references/upstream/linux/include/linux/netdevice.h#L2659) `WARN_ON((dev)->reg_state != NETREG_UNINITIALIZED);	\`。
  - 登记用的编号在同一台 devlink 设备里不许重复：[net/devlink/port.c 第 1068 行](../../../references/upstream/linux/net/devlink/port.c#L1068) `int devl_port_register_with_ops(struct devlink *devlink,`。口号内核不查范围、不查重复，只用来给用户看和拼物理口名，物理口名是 p 加口号：[net/devlink/port.c 第 1519 行](../../../references/upstream/linux/net/devlink/port.c#L1519) `n += snprintf(name + n, len - n, "p%u",`。
  - 物理口名只出现在 `/sys/class/net/<口>/phys_port_name` 和 `ip` 命令的网口消息里；改网口的名字内核不看 devlink（资料文件决定-001"内核自己怎样规定"）。
- 别家怎样做（数出自资料文件决定-001 的"归类的结果"，26 处，一处可以归进几类）：登记成物理口的 20 处，例如 [drivers/net/ethernet/intel/ice/devlink/port.c 第 345 行](../../../references/upstream/linux/drivers/net/ethernet/intel/ice/devlink/port.c#L345) `attrs.phys.port_number = pf->hw.pf_id;`、[drivers/net/ethernet/mscc/ocelot_net.c 第 182 行](../../../references/upstream/linux/drivers/net/ethernet/mscc/ocelot_net.c#L182) `attrs.phys.port_number = port;`、[net/dsa/devlink.c 第 325 行](../../../references/upstream/linux/net/dsa/devlink.c#L325) `attrs.phys.port_number = dp->index;`（口号都填口在硬件里的编号）；PCI 功能一类的口 6 处；虚拟的口 3 处；不填属性的 1 处。
- 高通的代码和 OpenWrt 上游的默认行为：高通的几处代码都不用 devlink。OpenWrt 上游的镜像上，这块板子的驱动是照 DSA 写的，DSA 框架替它登记 devlink 端口，口号填口在交换芯片里的序号；`lan4`、`lan3`、`lan2`、`wan`、`lan1` 的物理口名依次是 p2、p3、p4、p5、p6（资料文件决定-001"OpenWrt 上游的镜像"）。OpenWrt 上游的基础脚本里搜 `phys_port_name`，零处。
- 新驱动里的做法：没有登记 devlink 端口。口号出自设备树里每个口的 `reg`，[新驱动的 ppe_netdev.c 第 192 行](../../../worktrees/openwrt-nss-adaptation-workrepo/target/linux/qualcommax/files/drivers/net/ethernet/qualcomm/ipq807x/ppe_netdev.c#L192) `ret = of_property_read_u32(np, "reg", &port_id);`；这块板子的设备树里 2 号口是 `lan4`，[worktrees/openwrt-nss-adaptation-workrepo/target/linux/qualcommax/dts/ipq8074-wxr-5950ax12.dts 第 275 行](../../../worktrees/openwrt-nss-adaptation-workrepo/target/linux/qualcommax/dts/ipq8074-wxr-5950ax12.dts#L275) `label = "lan4";`。

**每一种做得成的做法**

- 做法一：登记成物理口，口号和登记用的编号都用 PPE 里这个口的编号。
  - 驱动怎样做：建每个口的网口时，先建这个口的 devlink 端口、连到网口上，再登记网口。
  - 用户看到什么：`devlink port show` 里五行，编号 2 到 6；`phys_port_name` 是 p2 到 p6。
- 做法二：登记成物理口，口号照机壳上的次序另外编（例如 `wan` 是 0，`lan1` 到 `lan4` 是 1 到 4）。
  - 用户看到什么：`phys_port_name` 变成 p0 到 p4，和 OpenWrt 上游的镜像不一样。
- 做法三：口号照硬件，登记用的编号从 0 起连着排。
  - 用户看到什么：`devlink port show` 里编号是 0 到 4，物理口名是 p2 到 p6，同一个口两个号。

**演绎**

1. 这件事要保证两样：用户从 devlink 端口认得出是硬件的哪个口；从 OpenWrt 上游的镜像换过来，已经有的名字不变。
2. 种类：这五个口都是接着网线的口，内核给这种口的种类就是"物理口"；上游 26 处里 20 处这样填。没有第二种填法。
3. 做法二：口号变了，物理口名跟着变，保证不了第二样；机壳上的次序驱动也不知道，要另写进设备树。否掉。
4. 做法三：同一个口有两个号，用户要自己记对应；保证不了第一样里的"认得出"。上游的例子里口号和登记用的编号用的都是硬件里的编号。否掉。
5. 剩下做法一。它和 DSA 框架在 OpenWrt 上游的镜像上填的一样。
6. 有没有比各家都好的做法：没有找到。
7. 这个结论在什么情况下是错的：路由器上有程序照 `phys_port_name` 改网口的名字，登记了端口以后名字被改掉。OpenWrt 上游的镜像上这个名字本来就有，基础脚本里没有读它的地方；网口的名字是驱动照设备树的 `label` 起的。读源码排除了。

**去处和理由**

自己定。做法二、做法三各有一条出自资料的理由被否掉；做法一用户看到的和 OpenWrt 上游的镜像一样。

**结论和设计说明**

驱动给五个有网口的口各登记一个 devlink 端口，种类是物理口，口号就是 PPE 里的口号。路由器上 `devlink port show` 列出五行，每一行写着它连着哪个网口；`/sys/class/net/lan1/phys_port_name` 读到 p6，别的口照此类推。网口的名字（`lan1` 到 `lan4`、`wan`）不变。

## 决定-002　"属于哪台交换机"的编号（`switch_id`）填不填、填什么

去处：要用户定。

**资料归类以后的结果**

- 内核怎样规定：
  - 端口属性里填了这个编号，内核才把这个端口记成交换机的口：[net/devlink/port.c 第 1349 行](../../../references/upstream/linux/net/devlink/port.c#L1349) `devlink_port->switch_port = true;`；没有填的，问网口"属于哪台交换机"得到"不支持"：[net/devlink/port.c 第 1603 行](../../../references/upstream/linux/net/devlink/port.c#L1603) `if (!devlink_port || !devlink_port->switch_port)`。
  - 内核里用这个编号的地方（资料文件决定-002"内核自己怎样规定"的第 9、10 问）：给用户看（`phys_switch_id` 文件、`ip` 命令的网口消息）；网桥只在驱动自己调了"这个桥口由硬件转发"的函数以后才取它；组播转发只在包带着"硬件转发过了"的记号时才比它；DSA 在网口加进网桥时拿它做一次检查。驱动不调那个函数、不给包打记号，这些地方都不起作用。
- 别家怎样做（数出自资料文件决定-001 的"归类的结果"，26 处）：填的 15 处，例如 [drivers/net/ethernet/broadcom/bnxt/bnxt_devlink.c 第 1297 行](../../../references/upstream/linux/drivers/net/ethernet/broadcom/bnxt/bnxt_devlink.c#L1297) `memcpy(attrs.switch_id.id, bp->dsn, sizeof(bp->dsn));`、[drivers/net/ethernet/intel/ixgbe/devlink/devlink.c 第 522 行](../../../references/upstream/linux/drivers/net/ethernet/intel/ixgbe/devlink/devlink.c#L522) `u64 id = pci_get_dsn(adapter->pdev);`（填的是设备的序列号）；不填的 11 处，例如 [drivers/net/can/kvaser_pciefd/kvaser_pciefd_devlink.c 第 41 行](../../../references/upstream/linux/drivers/net/can/kvaser_pciefd/kvaser_pciefd_devlink.c#L41) `struct devlink_port_attrs attrs = {`。填的内容有网卡的地址、序列号、寄存器的地址、一个序号几种。
- 高通的代码和 OpenWrt 上游的默认行为：OpenWrt 上游的镜像上 DSA 框架填的是 DSA 树的编号，这块板子上 `phys_switch_id` 读出来是 00000000，五个口一样（资料文件决定-002"OpenWrt 上游的镜像"）。OpenWrt 上游的基础脚本里搜 `phys_switch_id`，零处。
- 新驱动里的做法：没有登记 devlink 端口，`phys_switch_id` 读出来是"不支持"。阶段 1 不做硬件转发。

**每一种做得成的做法**

- 做法一：不填。
  - 用户看到什么：读 `/sys/class/net/<口>/phys_switch_id` 报"不支持"；`ip -d link` 里没有 `switchid`。和 OpenWrt 上游的镜像不一样。
- 做法二：填，五个口填同一个编号，内容用 PPE 的寄存器起始地址（4 个字节）。
  - 用户看到什么：五个口的 `phys_switch_id` 读出来是同一个数；`ip -d link` 里有 `switchid`。有这一项和 OpenWrt 上游的镜像一样，数不一样。
- 做法三：填，内容照 OpenWrt 上游的镜像填 00000000。
  - 用户看到什么：和 OpenWrt 上游的镜像一字不差。
- 做法四：填，内容用网卡的地址。

**演绎**

1. 这件事要保证三样：填的东西说的是实情；不因为填了它让内核少转发包；从 OpenWrt 上游的镜像换过来，用户原来读得到的东西还读得到。
2. 实情：五个口是同一块 PPE 上的口。填同一个编号说的就是这件事，和驱动做不做硬件转发无关。
3. 第二样：内核用这个编号的四处里，会改变转发的三处都要驱动先调函数或者给包打记号；新驱动两样都不做。所以填了不会少转发：网桥要等驱动调了"这个桥口由硬件转发"的函数才看这个编号，只填编号它不看。
4. 做法一保证不了第三样：OpenWrt 上游的镜像上读得到，换过来读不到。它的理由只能是"不做硬件转发就不该填"，而第 2 步说了这个编号说的是硬件，不是功能。
5. 做法四：网卡的地址会经这个文件多露出一处，没有必要。否掉。
6. 做法三：00000000 是 DSA 给第 0 棵树的编号。以后这台路由器上要是再接一个照 DSA 写的交换芯片，它也会是 00000000，内核会把两块不相干的硬件认成同一台交换机。否掉。
7. 做法二：寄存器的起始地址一块 PPE 一个，不会和别的硬件重。上游填的 15 处里用寄存器地址、序列号这类"这块硬件独有的数"。
8. 有没有比各家都好的做法：没有找到。
9. 这个结论在什么情况下是错的：有程序看到 `phys_switch_id` 就认定这几个口之间的转发已经交给了硬件。内核里没有这样的地方（第 3 步）；OpenWrt 上游的基础脚本里没有读它的地方。读源码排除了。

**去处和理由**

要用户定。剩下做法一和做法二，差的是取舍（不做硬件转发时要不要说"这五个口在同一块交换硬件上"）；用户看得见，而且两种都和 OpenWrt 上游的镜像不完全一样。

**建议**

做法二。依据是演绎第 2、3、6、7 步。

**结论和设计说明**

五个口的 devlink 端口属性里都带同一个"属于哪台交换机"的编号，内容是 PPE 的寄存器起始地址。用户读 `phys_switch_id` 时五个口读到同一个数，知道它们在同一块交换硬件上。驱动在阶段 1 不把任何转发交给硬件，内核照旧在软件里转发每一个包。

**你的回答**

> 做法二

## 决定-003　没有网口的口（通 CPU 的口、芯片里有但是板子上没有用的口）登记不登记 devlink 端口

去处：要用户定。

**资料归类以后的结果**

- 内核怎样规定：登记了端口却一直不连网口，一小时以后内核记一行警告，[net/devlink/port.c 第 992 行](../../../references/upstream/linux/net/devlink/port.c#L992) `#define DEVLINK_PORT_TYPE_WARN_TIMEOUT (HZ * 3600)`；种类是"通 CPU 的口""交换芯片之间的口""没有用的口"的不警告。这三种端口要是连了网口，读物理口名时内核会警告：[net/devlink/port.c 第 1531 行](../../../references/upstream/linux/net/devlink/port.c#L1531) `WARN_ON(1);`。
- 别家怎样做（数出自资料文件决定-001 的"归类的结果"，26 处，一处可以归进几类）：这块硬件没有这样的口的 18 处；有这样的口而都不登记的 4 处；给通 CPU 的口登记的 2 处（DSA 框架 [net/dsa/devlink.c 第 325 行](../../../references/upstream/linux/net/dsa/devlink.c#L325) `attrs.phys.port_number = dp->index;`、Mellanox 的交换芯片 [drivers/net/ethernet/mellanox/mlxsw/core.c 第 3092 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlxsw/core.c#L3092) `err = devl_port_register_with_ops(devlink, devlink_port, local_port,`）；给没有用的口登记的 3 处，例如 [drivers/net/ethernet/mscc/ocelot_net.c 第 182 行](../../../references/upstream/linux/drivers/net/ethernet/mscc/ocelot_net.c#L182) `attrs.phys.port_number = port;`。
- 高通的代码和 OpenWrt 上游的默认行为：OpenWrt 上游的镜像上 DSA 框架把 8 个口都登记了：0 号是通 CPU 的口，2 到 6 号是物理口，1 号、7 号是没有用的口（资料文件决定-003"OpenWrt 上游的镜像"）。
- 新驱动里的做法：没有登记 devlink 端口。新驱动不是照 DSA 写的，PPE 的 0 号口（通 CPU）没有网口，主机直接经 EDMA 的环收发。

**每一种做得成的做法**

- 做法一：只给五个有网口的口登记。
  - 用户看到什么：`devlink port show` 五行。
- 做法二：8 个口都登记，0 号登记成"通 CPU 的口"，1 号、7 号登记成"没有用的口"。
  - 用户看到什么：`devlink port show` 八行，和 OpenWrt 上游的镜像一样；多出来的三行没有网口，也没有可以对它们做的操作。
- 做法三：五个有网口的口加 0 号口，一共六行。

**演绎**

1. 这件事要保证一样：`devlink port show` 里的每一行，用户都能拿它做点什么或者看出点什么。
2. 照决定-009 的建议，端口不带回调表，设备的回调表里也没有共用缓冲那一组回调（门限写死，见第四步设计第九节决定-007）。所以一个没有网口的端口，用户在它上面什么都做不了、什么都读不到。做法二多出来的三行、做法三多出来的一行都是这样。
3. 上游给通 CPU 的口登记的 2 处：DSA 框架是给每一种口都登记；Mellanox 的驱动是因为通 CPU 的口在共用缓冲里有自己的门限要给用户看。新驱动两样都不是。
4. 做法二的好处只有一条：行数和 OpenWrt 上游的镜像一样。那三行在上游的镜像上也是什么都做不了的。
5. 所以推理留下做法一。以后登记了共用缓冲那一组回调，通 CPU 的口有东西可看了，再给它登记，那时是加一行，不用改已经有的五行。
6. 有没有比各家都好的做法：没有找到。
7. 这个结论在什么情况下是错的：有程序数 `devlink port show` 的行数，或者要找 0 号口。OpenWrt 上游的基础脚本里搜 `devlink`，没有读端口的地方（决定-010 核过带不带这个命令）。读源码排除了。

**去处和理由**

要用户定。行数用户看得见，和 OpenWrt 上游的镜像不一样（五行对八行）。

**建议**

做法一。依据是演绎第 2、3、4 步。

**结论和设计说明**

驱动只给 `lan1` 到 `lan4`、`wan` 这五个口登记 devlink 端口。PPE 里通 CPU 的 0 号口和这块板子没有接出来的 1 号、7 号口不登记，`devlink port show` 里没有它们。

**你的回答**

> 做法一。以后登记了共用缓冲那一组回调，通 CPU 的口有东西可看了，再给它登记，那时是加一行，不用改已经有的五行。

## 决定-004　通道数（`lanes`）、能不能拆（`splittable`）填不填

去处：自己定。

**资料归类以后的结果**

- 内核怎样规定：通道数只在不是 0 的时候发给用户看，[net/devlink/port.c 第 236 行](../../../references/upstream/linux/net/devlink/port.c#L236) `if (attrs->lanes) {`；用户发"拆口"的命令时，内核先看端口有没有拆口的回调，没有就回"不支持"，[net/devlink/port.c 第 867 行](../../../references/upstream/linux/net/devlink/port.c#L867) `if (!devlink_port->ops->port_split)`，有了再看"能不能拆"和份数。内核不拿这两样做别的事。
- 别家怎样做（数出自资料文件决定-001 的"归类的结果"，26 处，一处可以归进几类）：两样都不填的 22 处，例如 [drivers/net/ethernet/broadcom/bnxt/bnxt_devlink.c 第 1266 行](../../../references/upstream/linux/drivers/net/ethernet/broadcom/bnxt/bnxt_devlink.c#L1266) `struct devlink_port_attrs attrs = {};`；填通道数的 4 处，例如 [drivers/net/ethernet/netronome/nfp/nfp_devlink.c 第 351 行](../../../references/upstream/linux/drivers/net/ethernet/netronome/nfp/nfp_devlink.c#L351) `attrs.lanes = eth_port.port_lanes;`；填"能不能拆"的 3 处，例如 [drivers/net/ethernet/mellanox/mlxsw/core.c 第 3074 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlxsw/core.c#L3074) `attrs.lanes = lanes;`。填的几处都是一个插口里有几路、可以拆成几个口用的网卡和交换芯片。10 处端口回调表里登记拆口回调的做法在资料文件决定-009 的名单里。
- 高通的代码和 OpenWrt 上游的默认行为：OpenWrt 上游的镜像上 DSA 框架不填这两样（资料文件决定-004"已经看得到的几种做法"）。
- 新驱动里的做法：没有登记 devlink 端口。每个口用哪一种接法写在设备树里，一个口一路。新驱动里没有把一个口拆成几个口的代码，也没有读到做这件事的寄存器。

**每一种做得成的做法**

- 做法一：两样都不填，不登记拆口、合口的回调。
  - 用户看到什么：`devlink port show` 里没有 `lanes`；`devlink port split` 回"不支持"。
- 做法二：填通道数（每个口填 1），不填"能不能拆"。
  - 用户看到什么：每一行多一项 `lanes 1`。
- 做法三：两样都填，并且登记拆口、合口的回调。

**演绎**

1. 这件事要保证一样：端口属性里写的是这块硬件真有的东西。
2. 做法三：拆口要硬件能把一个口的几路分开当几个口用。这块板子每个口只有一路，新驱动里没有读到做这件事的寄存器，回调写不出来。否掉。
3. 做法二：通道数的用处只有两个，给用户看和给拆口的命令做上限。不拆口，后一个用处没有；前一个用处是告诉用户"1"，上游 26 处里只有能拆的 4 处才填。否掉。
4. 剩下做法一，上游 22 处这样做。
5. 有没有比各家都好的做法：没有找到。
6. 这个结论在什么情况下是错的：这块硬件其实能把一个口拆开（例如把一路万兆拆成几路千兆）。新驱动和高通的 qca-nss-dp 里都没有读到；真有的话是另一块板子的事。

**去处和理由**

自己定。做法二、做法三各有一条出自资料的理由被否掉；做法一和 OpenWrt 上游的镜像一样。

**结论和设计说明**

端口属性里不写通道数，也不写能不能拆。用户对这几个口执行 `devlink port split` 会得到"不支持"。

## 决定-005　健康报告设几个、各管哪几种故障、挂在设备上还是挂在端口上

去处：要用户定。

**资料归类以后的结果**

- 内核怎样规定：
  - 健康报告可以建在设备上，[net/devlink/health.c 第 190 行](../../../references/upstream/linux/net/devlink/health.c#L190) `devl_health_reporter_create(struct devlink *devlink,`，也可以建在端口上，[net/devlink/health.c 第 145 行](../../../references/upstream/linux/net/devlink/health.c#L145) `devl_port_health_reporter_create(struct devlink_port *port,`；建在端口上的，用户要给出端口号才找得到。
  - 内核的文档写的是驱动给每一种故障建一个健康报告（资料文件决定-005"内核自己怎样规定"）。
  - 一个报告只有一个状态（健康、出错）、一个恢复的回调、一个宽限期。开着自动恢复的报告，上一次的错还没有恢复时，新报的错只加次数，内核不再调恢复的回调：[net/devlink/health.c 第 639 行](../../../references/upstream/linux/net/devlink/health.c#L639) `if (prev_state != DEVLINK_HEALTH_REPORTER_STATE_HEALTHY)`。所以一个报告管着几种错的时候，其中一种把报告停在"出错"，别的几种也不再自动恢复（决定-007）。
  - 驱动可以自己调函数把报告的状态改回"健康"或者改成"出错"：[net/devlink/health.c 第 698 行](../../../references/upstream/linux/net/devlink/health.c#L698) `devlink_health_reporter_state_update(struct devlink_health_reporter *reporter,`。这个函数要发 netlink 通知，会睡眠（[net/devlink/health.c 第 521 行](../../../references/upstream/linux/net/devlink/health.c#L521) `msg = nlmsg_new(NLMSG_DEFAULT_SIZE, GFP_KERNEL);`），不能在轮询里调。
- 别家怎样做（数出自资料文件决定-005 的"归类的结果"，18 处报告的定义）：挂在设备上的 15 处，例如 [drivers/net/ethernet/meta/fbnic/fbnic_devlink.c 第 587 行](../../../references/upstream/linux/drivers/net/ethernet/meta/fbnic/fbnic_devlink.c#L587) `.name = "otp",`、[drivers/net/ethernet/huawei/hinic/hinic_devlink.c 第 437 行](../../../references/upstream/linux/drivers/net/ethernet/huawei/hinic/hinic_devlink.c#L437) `.name = "fw",`；挂在端口上的 3 处，例如 [drivers/net/ethernet/mellanox/mlx5/core/en/reporter_rx.c 第 672 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlx5/core/en/reporter_rx.c#L672) `.name = "rx",`（收的队列是每个口自己的）。有恢复的回调的 8 处；没有恢复的回调、也不改状态的 9 处；没有恢复的回调、驱动自己改状态的 1 处。
  - 一个报告管多大一块：Marvell 的驱动一根报错的中断线一个报告，一个报告管这根线上的好几位，[drivers/net/ethernet/marvell/octeontx2/af/rvu_devlink.c 第 485 行](../../../references/upstream/linux/drivers/net/ethernet/marvell/octeontx2/af/rvu_devlink.c#L485) `RVU_REPORTERS(hw_nix_intr);` 起的四行。没有读到一位一个报告的。
  - 驱动自己把报告改回"健康"的，整个驱动目录里搜 `devlink_health_reporter_state_update`，6 处、5 个文件、4 个驱动（资料文件决定-005"另外读到的资料"）。四个驱动都是在驱动自己把出毛病的东西重新弄好的那一刻调：固件复位做完，[drivers/net/ethernet/broadcom/bnxt/bnxt.c 第 15556 行](../../../references/upstream/linux/drivers/net/ethernet/broadcom/bnxt/bnxt.c#L15556) `bnxt_dl_health_fw_status_update(bp, true);`；固件重新起来，[drivers/net/ethernet/amd/pds_core/core.c 第 616 行](../../../references/upstream/linux/drivers/net/ethernet/amd/pds_core/core.c#L616) `devlink_health_reporter_state_update(pdsc->fw_reporter,`；网卡复位、重建做完，[drivers/net/ethernet/intel/ice/devlink/health.c 第 537 行](../../../references/upstream/linux/drivers/net/ethernet/intel/ice/devlink/health.c#L537) `devlink_health_reporter_state_update(reporter,`；收发的通道重新打开，[drivers/net/ethernet/mellanox/mlx5/core/en/health.c 第 78 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlx5/core/en/health.c#L78) `devlink_health_reporter_state_update(priv->tx_reporter,`。
- 高通的代码和 OpenWrt 上游的默认行为：都没有健康报告；OpenWrt 上游的镜像上 `devlink health show` 是空的。
- 新驱动里要告诉用户的故障（第四步设计文件第九节）：
  - 硬件报错的中断报的九种错。这根中断的状态读了不清、线一直拉着（直接在硬件上测出来的），所以报了的位在中断处理函数里一定要关掉；关掉的位怎样再打开是第四步设计第九节的决定-003。
  - 环上的账对不上的两样（收到的页不在驱动记着的表里、"发完"的登记上没有包）：出了就是出了，驱动没有可以恢复的动作。
  - 空缓冲区环空了：所有的收包环一起停；驱动在轮询里当场补，补上就恢复（直接在硬件上测出来的，第四步设计第九节决定-009）。
  - 硬件报错的中断进来、状态里没有认得的位：不是一种认得的故障。
  - 收包环、发完环、报错的中断都是所有的口共用的，分不到哪一个口上。

**每一种做得成的做法**

- 做法一：整块设备一个报告，管全部故障。
  - 驱动怎样做：建一个报告；哪一种故障都报给它。
  - 用户看到什么：`devlink health show` 一行；哪一种故障出了都是它"出错"。
- 做法二：两个报告，都挂在设备上：`edma_hw`（硬件报的九种错）、`edma_ring`（账对不上的两样）。空缓冲区环空了不进报告，只有计数和内核记录。
  - 驱动怎样做：建两个报告。
  - 用户看到什么：`devlink health show` 两行，各有自己的状态和出错次数。空缓冲区环空过没有，要看 `ethtool -S` 的计数。
- 做法三：照口分，每个口的端口上各挂报告。
  - 用户看到什么：每个口一行或者几行。
- 做法四：不设健康报告，只用内核记录和计数。
  - 用户看到什么：`devlink health show` 是空的，和 OpenWrt 上游的镜像一样。
- 做法五：三个报告，都挂在设备上：做法二的两个，再加 `edma_rxfill` 管空缓冲区环空了。
  - 驱动怎样做：轮询发现空缓冲区环空了，排工作，工作里向 `edma_rxfill` 上报；轮询把缓冲补上以后再排一次工作，工作里调内核的函数把 `edma_rxfill` 改回"健康"。`edma_rxfill` 没有恢复的回调。
  - 用户看到什么：`devlink health show` 三行。空缓冲区环空着的那一段，`edma_rxfill` 是"出错"；补上以后回到"健康"，出错次数留着，头一次空的时候存下的现场和时间也留着。
- 做法六：硬件报的九种错各一个报告，再加账对不上、空缓冲区环空了的报告，一共十一个。
  - 驱动怎样做：建十一个报告；中断处理函数照报了的是哪一位，报给哪一个。
  - 用户看到什么：`devlink health show` 十一行。一种错把它自己的报告停在"出错"，不连累别的八种错自动恢复。

**演绎**

1. 这件事要保证四样：故障出了，用户不翻内核记录也看得到"出过错"；能恢复的故障恢复了，不把不能恢复的故障"出过错"这件事盖掉；报告里的东西分得到它挂的那个对象上；报告的状态说的是实情。
2. 做法三保证不了第三样：这几种故障出在所有的口共用的环和中断上，分不到口。上游挂在端口上的 3 处，它们的队列是每个口自己的。否掉。
3. 做法一保证不了第二样：一个报告只有一个状态。硬件报的错一恢复，状态回到"健康"，同一个报告里账对不上的那一次就看不出来了。恢复办法不一样的故障不能放进同一个报告。否掉。
4. 做法四保证不了第一样：账对不上的那两样，限速的内核记录会被冲掉，出事那一刻的现场留不下来。否掉。
5. 剩下做法二、做法五、做法六，都是"恢复办法不一样的分开"。
6. 做法二和做法五差在空缓冲区环空了进不进报告。空缓冲区环一空，所有的口都收不了包，是这个驱动运行时最重的一种情形。进了报告，驱动补上以后要自己把状态改回"健康"，内核有这个函数，上游四个驱动都是在自己把出毛病的东西弄好的那一刻调它，这里的情形（驱动自己把缓冲补上）是同一种用法；状态说的是实情。多出来的东西是：这一段时间报告是"出错"，头一次空的时候的现场和时间留得下来，状态变的时候有通知。不进报告，这些都没有，只有次数和限速的内核记录。两种都做得成，没有哪一条资料否掉哪一种。
7. 做法五和做法六差在九种错合成一个报告还是拆成九个。合成一个的代价出自内核的算法：一种错把 `edma_hw` 停在"出错"以后，别的八种错来了，位关掉、记一行，内核不再自动打开，要等用户执行恢复的命令。拆成九个没有这个代价，代价是十一行、九个差不多一样的报告。读数里一直报着的错只有"发包的内存满了"一种，它出了以后所有口的发送都停、重新内存引导才好，那时别的错还自动恢复不恢复已经不要紧；别的八种错会不会一直报着，没有读数。上游读到的是一根中断线一个报告，没有一位一个报告的。两种都做得成，差的是取舍。
8. 状态里没有认得的位只计数：它不是一种认得的故障，没有"出错"和"恢复"可说。
9. 有没有比各家都好的做法：没有找到；这里做的是照内核文档的话（一种故障一个报告）分，分的标准用"恢复的办法一样不一样"。
10. 这个结论在什么情况下是错的：
   - 账对不上的两样其实有办法恢复（例如复位这个环）。两次上机没有出现过这两样，没有读数说明复位救得了；读源码定不下来。
   - 别的八种错里有会一直报着的，而且出得不少：那时合成一个报告的代价是真的，该选做法六。E-0006 只造得出"发包的内存满了"这一种，排除不了。

**去处和理由**

要用户定。`devlink health show` 里多出几行，用户看得见，和 OpenWrt 上游的镜像不一样；剩下的三种做法差的是取舍。

**建议**

做法五。依据是演绎第 3、4、6、7 步：恢复办法不一样的三类故障各一个报告；空缓冲区环空了是最重的一种情形，值得留下状态和现场；九种错合成一个报告，和上游的分法一样，代价写在第 7 步。

**结论和设计说明**

驱动加载时在 devlink 设备上建三个健康报告。`edma_hw`：硬件报错的中断报了九种错里的哪一种，它就变成"出错"；恢复以后回到"健康"。`edma_ring`：收到的页对不上、"发完"的登记对不上包，出一次它就变成"出错"并记下现场，以后一直是"出错"，出错次数接着加。`edma_rxfill`：空缓冲区环空了，它变成"出错"并记下现场；驱动把缓冲补上以后，它回到"健康"，出错次数留着。用户用 `devlink health show` 看这三行。

**你的回答**

> 好的, 做法5.

## 决定-006　故障在哪里交给健康报告；同一种故障连着来时怎样算

去处：自己定。

**资料归类以后的结果**

- 内核怎样规定：
  - 上报的函数是 [net/devlink/health.c 第 655 行](../../../references/upstream/linux/net/devlink/health.c#L655) `int devlink_health_report(struct devlink_health_reporter *reporter,`。它先把出错次数加一、把状态改成"出错"，[net/devlink/health.c 第 665 行](../../../references/upstream/linux/net/devlink/health.c#L665) `reporter->error_count++;`、[net/devlink/health.c 第 667 行](../../../references/upstream/linux/net/devlink/health.c#L667) `reporter->health_state = DEVLINK_HEALTH_REPORTER_STATE_ERROR;`；然后要拿 devlink 的锁去存现场、去恢复，[net/devlink/health.c 第 680 行](../../../references/upstream/linux/net/devlink/health.c#L680) `devl_lock(devlink);`。拿锁会睡眠，所以不能在中断处理函数和轮询里调。
  - 驱动自己改报告状态的函数要发 netlink 通知，也会睡眠：[net/devlink/health.c 第 521 行](../../../references/upstream/linux/net/devlink/health.c#L521) `msg = nlmsg_new(NLMSG_DEFAULT_SIZE, GFP_KERNEL);`。
  - 开着自动恢复的报告，上一次的错还没有恢复时，新的上报只加次数，不存现场、不恢复：[net/devlink/health.c 第 639 行](../../../references/upstream/linux/net/devlink/health.c#L639) `if (prev_state != DEVLINK_HEALTH_REPORTER_STATE_HEALTHY)`、[net/devlink/health.c 第 670 行](../../../references/upstream/linux/net/devlink/health.c#L670) `if (devlink_health_recover_abort(reporter, prev_health_state)) {`。自动恢复关着的报告，每一次上报都不中止，没有存着现场时存一份：[net/devlink/health.c 第 635 行](../../../references/upstream/linux/net/devlink/health.c#L635) `if (!reporter->auto_recover)`。
  - 上报函数自己不往内核记录里写，只发 netlink 通知和跟踪事件（资料文件决定-006"内核自己怎样规定"）。
- 硬件上量到的（直接在硬件上测出来的）：报错的中断的状态读了不清，线一直拉着；处理函数不关的话这根中断一遍进了 4569407 次（[上机实验/E-0004/报告.md 第 401 行](../../上机实验/E-0004/报告.md#L401)）。
- 别家怎样做（数出自资料文件决定-006 的"归类的结果"，26 处上报的地方）：
  - 在哪里报：排出去的工作里报的 16 处，例如 [drivers/net/ethernet/marvell/octeontx2/af/rvu_devlink.c 第 77 行](../../../references/upstream/linux/drivers/net/ethernet/marvell/octeontx2/af/rvu_devlink.c#L77) `queue_work(rvu_dl->devlink_wq, &rvu_dl->rvu_nix_health_reporter->intr_work);`（中断处理函数读出状态、存起来、排工作）；固件事件的回调里报的 4 处；定时器或者定时的工作里报的 2 处；别的 3 处。
  - 连着来怎样处理：每次都报的 10 处；关掉来源、恢复以后再开的 8 处；合并或者限速的 7 处，例如 [drivers/net/ethernet/mellanox/mlx5/core/en/health.c 第 156 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlx5/core/en/health.c#L156) `netdev_err(priv->netdev, "%s\n", err_str);`。
- 高通的代码和 OpenWrt 上游的默认行为：没有健康报告。
- 你定过的：硬件报的每一种错在内核记录里记一行（[各阶段设计/阶段一/3-收发.md 第 88 行](../../各阶段设计/阶段一/3-收发.md#L88)）。

**每一种做得成的做法**

在哪里报：

- 做法一：中断处理函数或者轮询里只记下故障，排一个工作，在工作里报。
- 做法二：把报错的中断申请成带线程的中断，在线程函数里报。
- 做法三：由定时的检查发现故障再报。

同一种故障连着来：

- 做法甲：每来一次报一次。
- 做法乙：报了以后来源已经关掉，再打开以后才会再来。
- 做法丙：驱动合并，几次故障只报一次。

**演绎**

1. 这件事要保证三样：上报不在不许睡眠的地方做；故障连着来时不把核心占住、不把内核记录刷满；出了几次用户查得到。
2. 做法三要定时的检查，是没有授权的定时的事；故障本来就有中断和轮询在当场发现。否掉。
3. 做法二只管得了报错的中断。账对不上、空缓冲区环空了是在轮询里发现的，轮询不许睡眠，还是要排工作。两种故障用两套办法，不如都用工作。上游 26 处里没有一处是在带线程的中断的线程函数里报，16 处用工作。否掉。
4. 剩下做法一。工作排在系统的工作队列里，卸载时先取消并等它做完，再销毁报告。
5. 硬件报的错连着来：这根中断的状态读了不清、线一直拉着，所以处理函数返回以前一定要把报了错的位关掉，不然马上再进来（量到过四百多万次）。这是硬件的读数定的，和第四步设计第九节决定-003 选哪一种做法无关：决定-003 里做得成的几种做法都在处理函数里关掉报了错的位，差别只在关几位、以后打不打开。位关着的时候这种错不会再进中断，所以硬件报的错只能是做法乙；做法甲在这块硬件上没有"每来一次"可说。上游 8 处是做法乙。每一种错记的那一行内核记录也在工作里写。
6. 账对不上、空缓冲区环空了连着来：轮询里每出一次都排工作、都上报的话，出得密时工作会排个不停（做法甲），保证不了第二样；量到过空缓冲区环空着的时候一遍记了 160 行。所以用做法丙：轮询里只把次数加一；"还没有报过的现场"是空的时候才存下这一次的现场、排工作；工作报一次。准确的次数由轮询里的计数给（给不给用户看、放在哪里，是第四步设计第九节决定-009 问你的事），健康报告的出错次数记的是报了几回。上游 7 处合并或者限速。
7. 有没有比各家都好的做法：没有找到。
8. 这个结论在什么情况下是错的：工作被拖得很晚，用户看到"出错"比故障晚很多。内核不保证工作多久以内跑：普通的工作队列只管入队，没有最晚执行的时间（[编译用的内核 6.18.44 的 kernel/workqueue.c 第 2513 行](../../../worktrees/openwrt-nss-adaptation-workrepo/build_dir/target-aarch64_cortex-a53_musl/linux-qualcommax_ipq807x/linux-6.18.44/kernel/workqueue.c#L2513) `void delayed_work_timer_fn(struct timer_list *t)`）。拖多久没有量过；内核记录里的那一行是工作里写的，时间看得出来。

**去处和理由**

自己定。做法二、做法三各有一条出自资料的理由被否掉；硬件报的错只能是做法乙，出自硬件的读数；轮询里发现的故障的做法甲被"工作排个不停"否掉。这几条理由都不靠别的还没有回答的决定。设几个报告、各叫什么，在决定-005 里问你；这里定的只是驱动里面在哪里调上报的函数。

**结论和设计说明**

硬件报错的中断进来时，中断处理函数读状态、把报了错的位关掉、把这些位记进一个变量、排工作，马上返回。工作里：每一种错往内核记录里记一行，然后向 `edma_hw` 上报一次，报的话里写明是哪几种错。收包、收"发完"的轮询里发现账对不上时，把次数加一；头一次还把现场（哪个环、哪个号、描述符里的字）存下来、排同一个工作，工作向 `edma_ring` 上报一次。收包的轮询发现空缓冲区环空了，把次数加一，头一次排工作，工作向 `edma_rxfill` 上报；轮询把缓冲补上以后再排一次工作，工作把 `edma_rxfill` 改回"健康"。

## 决定-007　报告怎样回到"健康"：恢复的回调、宽限期、自动恢复

去处：要用户定。

**资料归类以后的结果**

- 内核怎样规定（`net/devlink/health.c`，每一行都是打开函数体读的）：
  - 报告有恢复的回调，自动恢复默认就开着：[net/devlink/health.c 第 131 行](../../../references/upstream/linux/net/devlink/health.c#L131) `reporter->auto_recover = !!ops->recover;`。
  - 每一次上报，内核先把出错次数加一、记下报告原来的状态、把状态改成"出错"：[net/devlink/health.c 第 665 行](../../../references/upstream/linux/net/devlink/health.c#L665) `reporter->error_count++;`、[net/devlink/health.c 第 666 行](../../../references/upstream/linux/net/devlink/health.c#L666) `prev_health_state = reporter->health_state;`、[net/devlink/health.c 第 667 行](../../../references/upstream/linux/net/devlink/health.c#L667) `reporter->health_state = DEVLINK_HEALTH_REPORTER_STATE_ERROR;`。
  - 然后判断这一次要不要中止。自动恢复关着的报告从不中止：[net/devlink/health.c 第 635 行](../../../references/upstream/linux/net/devlink/health.c#L635) `if (!reporter->auto_recover)`。自动恢复开着的报告，两种情形中止：报告原来的状态不是"健康"，[net/devlink/health.c 第 639 行](../../../references/upstream/linux/net/devlink/health.c#L639) `if (prev_state != DEVLINK_HEALTH_REPORTER_STATE_HEALTHY)`；离上一次恢复还不到"突发期加宽限期"，[net/devlink/health.c 第 648 行](../../../references/upstream/linux/net/devlink/health.c#L648) `if (reporter->last_recovery_ts && reporter->recovery_count &&`、[net/devlink/health.c 第 649 行](../../../references/upstream/linux/net/devlink/health.c#L649) `time_is_after_jiffies(recover_ts_threshold))`。
  - 中止的那一次，内核既不存现场也不调恢复的回调，直接返回：[net/devlink/health.c 第 670 行](../../../references/upstream/linux/net/devlink/health.c#L670) `if (devlink_health_recover_abort(reporter, prev_health_state)) {`。报告停在"出错"。
  - 没有中止的，内核先存现场，再当场调恢复的回调。回调返回成功，内核记下恢复的时刻、把状态改回"健康"：[net/devlink/health.c 第 575 行](../../../references/upstream/linux/net/devlink/health.c#L575) `devlink_health_reporter_recovery_done(reporter);`、[net/devlink/health.c 第 576 行](../../../references/upstream/linux/net/devlink/health.c#L576) `reporter->health_state = DEVLINK_HEALTH_REPORTER_STATE_HEALTHY;`；回调返回失败，状态留在"出错"：[net/devlink/health.c 第 573 行](../../../references/upstream/linux/net/devlink/health.c#L573) `return err;`。
  - 突发期是另一个数，驱动不给就是 0：[net/devlink/health.c 第 130 行](../../../references/upstream/linux/net/devlink/health.c#L130) `reporter->burst_period = ops->default_burst_period;`。它不是 0 的时候，头一次恢复以后的这一段时间里，内核每一次都自动恢复、不看宽限期：[net/devlink/health.c 第 642 行](../../../references/upstream/linux/net/devlink/health.c#L642) `if (devlink_health_reporter_in_burst(reporter))`。
  - 用户执行 `devlink health recover` 走的是同一个恢复的函数，不看宽限期：[net/devlink/health.c 第 725 行](../../../references/upstream/linux/net/devlink/health.c#L725) `return devlink_health_reporter_recover(reporter, NULL, info->extack);`。没有恢复的回调时得到"不支持"：[net/devlink/health.c 第 569 行](../../../references/upstream/linux/net/devlink/health.c#L569) `return -EOPNOTSUPP;`。
  - 宽限期的默认值由驱动在报告的回调表里给，[include/net/devlink.h 第 835 行](../../../references/upstream/linux/include/net/devlink.h#L835) `u64 default_graceful_period;`；用户可以用 `devlink health set` 改宽限期、开关自动恢复。
  - 驱动可以自己调函数把状态改回"健康"：[net/devlink/health.c 第 698 行](../../../references/upstream/linux/net/devlink/health.c#L698) `devlink_health_reporter_state_update(struct devlink_health_reporter *reporter,`。
- 从上面几行推出来的（推论，每一条出自哪几行写在后面）：
  - 内核没有"过一段时间自动再试一次"。报告一停在"出错"，以后每一次上报都被中止，直到用户执行恢复的命令，或者驱动自己把状态改回"健康"。（第 639 行）
  - 宽限期是照整个报告算的，不分是哪一种错。（第 648 行用的是报告的"上一次恢复的时刻"）
  - 宽限期越长，两次不相干的错越容易落在同一个宽限期里，后一次不恢复，报告停在"出错"。
  - 宽限期是 0 的时候，第 649 行的条件永远不成立：硬件一直报着的错会"恢复、再报、再恢复"转个不停。
- 别家怎样做：18 处报告的定义里，有恢复的回调的 8 处，例如 [drivers/net/ethernet/mellanox/mlx5/core/health.c 第 696 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlx5/core/health.c#L696) `.recover = mlx5_fw_fatal_reporter_recover,`；没有恢复的回调、也不改状态的 9 处；没有恢复的回调、驱动自己改状态的 1 处，[drivers/net/ethernet/amd/pds_core/main.c 第 207 行](../../../references/upstream/linux/drivers/net/ethernet/amd/pds_core/main.c#L207) `.diagnose = pdsc_fw_reporter_diagnose,`。驱动自己把报告改回"健康"的有 4 个驱动（决定-005）。别家默认的宽限期（用搜索取出来的数，只说明惯例的范围）：0，[drivers/net/ethernet/qlogic/qed/qed_devlink.c 第 90 行](../../../references/upstream/linux/drivers/net/ethernet/qlogic/qed/qed_devlink.c#L90) `#define QED_REPORTER_FW_GRACEFUL_PERIOD 0`；500 毫秒，[drivers/net/ethernet/mellanox/mlx5/core/en/reporter_rx.c 第 668 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlx5/core/en/reporter_rx.c#L668) `#define MLX5E_REPORTER_RX_GRACEFUL_PERIOD 500`；30 秒到 180 秒，[drivers/net/ethernet/mellanox/mlx5/core/health.c 第 673 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlx5/core/health.c#L673) `#define MLX5_FW_REPORTER_PF_GRACEFUL_PERIOD 60000`。
- 内核自己限速的内核记录，默认是 5 秒里最多 10 行：[include/linux/ratelimit_types.h 第 9 行](../../../references/upstream/linux/include/linux/ratelimit_types.h#L9) `#define DEFAULT_RATELIMIT_INTERVAL	(5 * HZ)`、[include/linux/ratelimit_types.h 第 10 行](../../../references/upstream/linux/include/linux/ratelimit_types.h#L10) `#define DEFAULT_RATELIMIT_BURST		10`。
- 高通的代码和 OpenWrt 上游的默认行为：没有健康报告。
- 硬件上量到的：硬件报了"发包的内存满了"以后状态一直留着，重新内存引导才好（第四步设计第九节决定-003）；别的八种错没有出现过，状态留不留着不知道。报了错的位关掉以后，状态寄存器里还读不读得到这一位、往状态寄存器写 1 能不能清，没有量过。

**每一种做得成的做法**

`edma_hw`：

- 做法一：有恢复的回调，自动恢复开着，宽限期不是 0；恢复的回调不看状态，把关着的位都打开。
  - 用户看到什么：一种错来了，内核记录一行，内核当场恢复，这一位重新打开，报告回到"健康"。硬件还报着这种错的话，马上再来一次：内核记录第二行，这一次离上一次恢复不到宽限期，内核不恢复，报告停在"出错"，这一位关着。从这时起别的错来了也只是关掉那一位、记一行。用户执行 `devlink health recover` 才再打开。
- 做法二：有恢复的回调，自动恢复开着，宽限期不是 0；恢复的回调先读状态寄存器（写 1 能清的话先清再读），还报着的位不打开、回"没有恢复成"，别的关着的位打开。
  - 用户看到什么：一种偶尔出一次的错来了，内核记录一行，内核当场恢复，报告回到"健康"。硬件一直报着的错来了，内核记录一行，恢复没有成，报告停在"出错"，这一位关着，不会再进一次中断。报告停在"出错"以后，别的错来了也只是关掉那一位、记一行，用户执行 `devlink health recover` 才再打开。
  - 这种做法要硬件在一位关掉以后，状态寄存器里还读得到这一位。读不到的话做不成，只能是做法一。
- 做法三：有恢复的回调，自动恢复默认关着。
  - 用户看到什么：每一种错来了，内核记录一行，这一位一直关着；报告是"出错"。用户执行 `devlink health recover` 才打开。
- 做法四：没有恢复的回调。
  - 用户看到什么：报过的位重新加载驱动才打开。
- 宽限期是 0 的自动恢复：硬件一直报着的错会转个不停，见演绎。

`edma_ring`：

- 做法甲：没有恢复的回调，出过错就一直"出错"。
- 做法乙：没有恢复的回调，驱动过一段时间没有再出就自己改回"健康"。
- 做法丙：给一个什么都不做的恢复回调，让用户能把它改回"健康"。

`edma_rxfill`（决定-005 选了做法五才有）：

- 做法子：没有恢复的回调；轮询把缓冲补上以后，驱动在工作里把它改回"健康"。
- 做法丑：没有恢复的回调，也不改状态：空过一次就一直"出错"。
- 做法寅：给一个恢复的回调，回调里补缓冲。

**演绎**

1. 这件事要保证四样：一种错出过以后，隔了一阵再出，不用用户动手也报得出来；硬件一直报着的错，不让恢复和上报转个不停；报告的状态说的是实情；一种错把报告停在"出错"的时候，少连累别的错。
2. `edma_hw` 的做法四保证不了第一样：报过的位再也不打开。否掉。
3. 宽限期是 0 的自动恢复保证不了第二样：恢复成功以后状态是"健康"，下一次上报两个中止的条件都不成立，内核又恢复；硬件一直报着的错每转一圈一行内核记录。否掉。
4. 做法三保证了第二、第三样，第一样要靠用户动手：路由器平常没有人盯着，一种偶尔出一次的错报过一回，这一位就一直关着。
5. 做法一、做法二里，偶尔出一次的错内核当场恢复，下一次还报得出来，条件是这一次离上一次恢复已经过了宽限期，而且报告没有停在"出错"。两次错（不管是不是同一种）隔得比宽限期近，后一次不恢复，报告停在"出错"，以后要用户动手。所以宽限期越短，第一样保证得越多。
6. 宽限期又不能太短。做法一里，硬件一直报着的错在恢复以后马上再进中断，从恢复到这第二次上报要经过"中断、排工作、工作跑起来"这一段；这一段比宽限期短，第二次才会被中止。这一段有多长，内核不保证上限：普通的工作队列只管入队，没有最晚执行的时间（[编译用的内核 6.18.44 的 kernel/workqueue.c 第 2513 行](../../../worktrees/openwrt-nss-adaptation-workrepo/build_dir/target-aarch64_cortex-a53_musl/linux-qualcommax_ipq807x/linux-6.18.44/kernel/workqueue.c#L2513) `void delayed_work_timer_fn(struct timer_list *t)`）；没有量过。真的比宽限期长的话，就每隔这样一段转一圈，一圈一行内核记录，最密也是一个宽限期一行。
7. 宽限期取 5 秒：平常"中断到工作"那一段远比 5 秒短（推论，没有量过）；万一转起来，最密 5 秒一行，比内核自己对限速的内核记录许的 5 秒 10 行还稀；两次不相干的错要隔得比 5 秒近才会把报告停住。别家的数从 0 到 180 秒都有，500 毫秒的是每条队列自己的报告，30 秒以上的是整块固件的报告。用户可以用 `devlink health set` 改。
8. 做法二比做法一少一次没有用的中断和上报：硬件一直报着的错只记一行，报告停在"出错"是恢复的回调自己说"没有恢复成"，不靠宽限期去拦。它保证第二样不靠"中断到工作那一段比宽限期短"这个内核不保证、也没有量过的前提。它做不做得成，要看一位关掉以后状态寄存器里还读不读得到这一位，读源码定不下来，E-0006 量。读得到，用做法二；读不到，用做法一。
9. 第四样：做法一、二、三里，报告一停在"出错"，别的错都不再自动恢复（内核的算法）。这是九种错合成一个报告带来的，拆不拆在决定-005。
10. `edma_ring`：做法乙要驱动定时去看，是定时的事，而且"过了一段时间没有再出"不等于好了。否掉。做法丙的恢复什么都没有恢复，只是把"出过错"擦掉，状态说的不是实情。否掉。留做法甲，上游 9 处这样做。没有恢复的回调，自动恢复就是关着的，每一次上报都不会被中止，次数照加，没有存着现场时存一份。
11. `edma_rxfill`：做法丑的状态不是实情（缓冲早就补上了）。否掉。做法寅：缓冲本来就由轮询当场补，再给用户一个"恢复"的命令没有东西可做。否掉。留做法子：上游 4 个驱动都是在自己把出毛病的东西弄好的那一刻把报告改回"健康"。
12. 有没有比各家都好的做法：做法二（恢复以前先看硬件还报不报）上游读到的恢复回调里没有这样写的，它是照这块硬件"状态读了不清"的读数想出来的。
13. 这个结论在什么情况下是错的：
   - "中断到工作"那一段比 5 秒长。E-0006 里读两行内核记录之间隔多久（只在用做法一时才有第二行）；超过 1 秒，就把宽限期加长。
   - 有一种错本来就隔几秒出一次。那时报告总停在"出错"，要用户动手；用户可以把宽限期改短。读数里没有这样的错。

**去处和理由**

要用户定。`edma_hw` 剩下"自动恢复开着"（做法一或者做法二，由硬件定）和"自动恢复关着"（做法三），差的是取舍；宽限期的数也是取舍；用户看得见。

**建议**

`edma_hw`：自动恢复开着，宽限期 5 秒，恢复的回调用做法二（E-0006 量出关掉的位读不到，就用做法一）。`edma_ring`：做法甲。`edma_rxfill`：做法子。依据是演绎第 4、5、7、8、10、11 步。

**结论和设计说明**

`edma_hw` 报了一种错，内核马上叫驱动恢复。驱动先看硬件还报不报着这种错：不报了，把这一位重新打开，报告回到"健康"，这种错以后再出照样有一行内核记录和一次上报；还报着，这一位留着不打开，报告停在"出错"。离上一次恢复不到 5 秒又来了一次错（哪一种都算），内核不再自动恢复，报告停在"出错"。报告停在"出错"以后，再来的错都只是关掉那一位、记一行；用户执行 `devlink health recover`，驱动再照上面的办法试一次。`edma_ring` 出过一次错就一直显示"出错"和出错的次数，重新加载驱动才回到"健康"。`edma_rxfill` 在空缓冲区环空着的时候是"出错"，驱动把缓冲补上以后回到"健康"，出错的次数留着。

**你的回答**

> edma_hw：自动恢复开着，宽限期 5 秒，恢复的回调用做法二（E-0006 量出关掉的位读不到，就用做法一）。edma_ring：做法甲。edma_rxfill：做法子。

## 决定-008　诊断、存现场、测试三个回调各给用户什么

去处：要用户定。

**资料归类以后的结果**

- 内核怎样规定：报告的回调表里除了恢复，还有存现场、诊断、测试三个回调，[include/net/devlink.h 第 827 行](../../../references/upstream/linux/include/net/devlink.h#L827) `int (*dump)(struct devlink_health_reporter *reporter,`、[include/net/devlink.h 第 830 行](../../../references/upstream/linux/include/net/devlink.h#L830) `int (*diagnose)(struct devlink_health_reporter *reporter,`、[include/net/devlink.h 第 833 行](../../../references/upstream/linux/include/net/devlink.h#L833) `int (*test)(struct devlink_health_reporter *reporter,`，都可以不填。有存现场的回调时，内核在上报的时候自动存一份，[net/devlink/health.c 第 132 行](../../../references/upstream/linux/net/devlink/health.c#L132) `reporter->auto_dump = !!ops->dump;`；已经存着一份时不再存，用户用 `devlink health dump clear` 清掉以后才存下一份。开着自动恢复的报告，被中止的那一次上报（报告本来就停在"出错"，或者离上一次恢复不到宽限期）不存现场：[net/devlink/health.c 第 670 行](../../../references/upstream/linux/net/devlink/health.c#L670) `if (devlink_health_recover_abort(reporter, prev_health_state)) {`。诊断是用户执行 `devlink health diagnose` 时当场调的。
- 别家怎样做（数出自资料文件决定-005 的"归类的结果"，18 处报告的定义，一处可以归进几类）：有存现场的 13 处；有诊断的 11 处；都没有的 1 处。整个驱动目录里搜健康报告的回调表里填了测试回调的，只有 1 处：[drivers/net/ethernet/mellanox/mlxsw/core.c 第 2019 行](../../../references/upstream/linux/drivers/net/ethernet/mellanox/mlxsw/core.c#L2019) `.test = mlxsw_core_health_fw_fatal_test,`，它发一条命令叫固件真的报一次致命错。
- 高通的代码和 OpenWrt 上游的默认行为：没有健康报告。
- 新驱动里读得到的东西：报错中断的状态寄存器和屏蔽寄存器；每一种错来过几次（要新加计数）；状态里没有认得的位来过几次（第四步设计第九节决定-004）；每个发包环、发完环、收包环、空缓冲区环读写的位置。

**每一种做得成的做法**

- 做法一：诊断、存现场都给，不给测试。
  - 用户看到什么：`devlink health diagnose` 给出这一刻的状态；`devlink health dump show` 给出头一次出错时存下的一份。
- 做法二：只给诊断。
  - 用户看到什么：只有这一刻的状态；出错那一刻是什么样，过后看不到。
- 做法三：只给存现场。
- 做法四：三个都不给。
  - 用户看到什么：只有"出错"和次数。
- 做法五：再加测试的回调（用户执行 `devlink health test` 时驱动假装报一次错）。

**演绎**

1. 这件事要保证两样：用户看到"出错"以后，不用重现就查得到出错那一刻是什么样；也查得到这一刻硬件是什么样。
2. 第一样要存现场：硬件报错以后状态可能被恢复清掉，账对不上的现场（哪个环、哪个号）过后就没有了，限速的内核记录也会被冲掉。做法二、做法四保证不了，否掉。
3. 第二样要诊断：屏蔽寄存器里哪几位关着、每一种错来过几次、状态里没有认得的位来过几次，这些是"这一刻"的东西；第四步设计第九节决定-001、决定-004 的建议都把要给用户看的数放在这里。做法三、做法四保证不了，否掉。
4. 做法五：上游只有 Mellanox 的交换芯片驱动 1 处填，它是叫固件真的走一遍出错的路。这块硬件没有读到能叫它假装报一次错的寄存器（用户给的资料也写：已经查过的 HPPE、EDMA 的定义里没有找到针对这九种错的注入字段）；驱动自己假装报一次，走的不是硬件那条路，测不出什么。真的错可以在上机时用实验用的模块造出来。否掉。
5. 剩下做法一。上游 18 处里有存现场的 13 处、有诊断的 11 处。
6. 每一样里放什么：
   - `edma_hw` 的诊断：状态寄存器、屏蔽寄存器此刻的值；九种错各来过几次；状态里没有认得的位来过几次；每个发包环、发完环读写的位置。
   - `edma_hw` 的存现场：上报时读到的状态、报了的是哪几位、那一刻每个发包环、发完环读写的位置。
   - `edma_ring` 的诊断：两样各出过几次（各个环的合计）。
   - `edma_ring` 的存现场：头一次出的是哪一样、哪个环、哪个号、描述符里的几个字。
   - `edma_rxfill`（决定-005 选了做法五才有）的诊断：每个空缓冲区环空过几次、此刻读写的位置；存现场：空的是哪个环、那一刻四个空缓冲区环和四个收包环读写的位置。
   - 现场里不放包的内容，也不放网卡的地址。
7. 有没有比各家都好的做法：没有找到。
8. 这个结论在什么情况下是错的：存现场要读的寄存器在出错以后读不得（读了会卡住）。量到过报"发包的内存满了"以后路由器还回话、读数脚本还读得了寄存器（第四步设计第九节决定-003 的读数）；别的八种错没有出现过，排除不了。

**去处和理由**

要用户定。`devlink health diagnose`、`devlink health dump show` 的内容用户看得见，OpenWrt 上游的镜像上没有。

**建议**

做法一，决定-005 定下几个报告就给几个。依据是演绎第 2、3、4 步。

**结论和设计说明**

用户执行 `devlink health diagnose <设备> reporter edma_hw`，看到报错中断此刻的状态、哪几位关着、九种错和认不出的状态各来过几次、每个发包环和发完环走到哪里。执行 `devlink health dump show`，看到头一次出错时内核自动存下的一份；看完用 `devlink health dump clear` 清掉，下一次出错再存。`edma_ring`、`edma_rxfill` 照此类推，各给上面演绎第 6 步列的那几样。

**你的回答**

> 做法一

## 决定-009　devlink 设备的回调表（`struct devlink_ops`）和端口的回调表（`struct devlink_port_ops`）各登记哪几个回调

去处：要用户定。

**资料归类以后的结果**

- 内核怎样规定：
  - devlink 设备的回调表里每一个回调都可以不填。设备信息的回调不填，内核照样回驱动的名字：[net/devlink/dev.c 第 890 行](../../../references/upstream/linux/net/devlink/dev.c#L890) `if (devlink->ops->info_get) {`、[net/devlink/dev.c 第 896 行](../../../references/upstream/linux/net/devlink/dev.c#L896) `err = devlink_nl_driver_info_get(devlink->dev_driver, &req);`。别的回调不填，对应的命令回"不支持"，或者什么都列不出（资料文件决定-009"内核自己怎样规定"）。端口的回调表也可以整张不给。
  - 重新加载的用处，内核的文档写的是三样：让"加载时才生效"的 devlink 参数和 devlink 资源的新值生效，[Documentation/networking/devlink/devlink-reload.rst 第 8 行](../../../references/upstream/linux/Documentation/networking/devlink/devlink-reload.rst#L8) ``` ``devlink-params`` and ``devlink-resources`` new values. It also provides ```；激活新的固件，[Documentation/networking/devlink/devlink-reload.rst 第 36 行](../../../references/upstream/linux/Documentation/networking/devlink/devlink-reload.rst#L36) ``` * - ``fw_activate`` ```；把 devlink 设备搬到另一个网络命名空间，[Documentation/networking/devlink/devlink-reload.rst 第 69 行](../../../references/upstream/linux/Documentation/networking/devlink/devlink-reload.rst#L69) `The netns option allows user to be able to move devlink instances into`。
- 别家怎样做（数出自资料文件决定-009 的"归类的结果"）：
  - 43 张设备的回调表里：填了设备信息的 27 张；刷写的 16 张（15 张刷的是设备自己的固件）；重新加载的 11 张；`eswitch_` 那一组的 8 张（7 张管虚拟功能和物理口之间怎样转发）；丢包原因那一组的 5 张；共用缓冲那一组的 4 张；限速那一组的 3 张；新建端口的 2 张；自检的 1 张；一个回调都没有填的 8 张。
  - 10 处端口的回调表：用在物理口上的 4 处，用在 PCI 功能一类的口上的 6 处；填的是拆口合口、改口的类型、改虚拟功能的网卡地址这几类（资料文件决定-009 的名单）。
- 高通的代码和 OpenWrt 上游的默认行为：高通的代码不用 devlink。OpenWrt 上游的镜像上 DSA 框架登记的回调表里设备信息的回调问到驱动那一头是空的，`devlink dev info` 只有驱动的名字（资料文件决定-009"OpenWrt 上游的镜像"）。
- 新驱动：没有 devlink 参数，没有 devlink 资源，阶段 1 没有固件，没有虚拟功能，没有自检。

**每一种做得成的做法**

一组一组地看，每一组都是"填"或者"不填"两种：

- 设备信息：填的话给出硬件的版本、固件的版本；不填，`devlink dev info` 只有驱动的名字。
- 重新加载：填的话 `devlink dev reload` 不卸载模块就把驱动重新初始化一遍；不填，这个命令回"不支持"。
- 刷写：填的话把一个固件文件写进设备。
- 共用缓冲：填的话 `devlink sb` 看和改缓冲的门限。
- 丢包原因：填的话 `devlink trap` 照原因看硬件丢了多少包；不填，`devlink trap show` 什么都列不出。
- `eswitch_`、限速、新建端口、自检：管虚拟功能、交换模式和自检。
- 端口的回调表：拆口合口、改口的类型、改虚拟功能的网卡地址。

**演绎**

1. 这件事要保证一样：登记了的每一个回调背后都有真的东西；没有的不登记，让命令老实地回"不支持"。
2. 刷写：这块硬件没有驱动要刷的固件。不填。
3. `eswitch_`、限速、新建端口：管的是虚拟功能，这块硬件没有。不填。自检：新驱动没有自检。不填。
4. 共用缓冲：第四步设计第九节决定-007、决定-014（去处都是"自己定"，理由出自资料）的结论是门限写死、用户不能改，没有要给用户改的东西。不填。
5. 重新加载：内核文档写的三样用处，新驱动一样都没有（没有加载时才生效的参数和资源，没有固件；搬网络命名空间阶段 1 没有要用的地方）。重新初始化要把所有的口停掉再建。上游 43 张里 11 张填。不填。
6. 设备信息：能填的只有硬件和固件的版本。PPE 的版本寄存器新驱动没有读，阶段 1 没有固件；内核文档不鼓励报驱动自己的版本（资料文件决定-009"内核自己怎样规定"）。不填的时候内核照样回驱动的名字，和 OpenWrt 上游的镜像上看到的一样。不填。
7. 端口的回调表：拆口不做（决定-004，去处是"自己定"），口的类型只有以太网一种，没有虚拟功能。不给。
8. 丢包原因：阶段 1 里硬件自己丢包的地方只有 PPE 的出口队列。这些丢的帧给不给用户看、从哪里看，是第四步设计第九节决定-012 问你的事，那里列的做法六就是"经 devlink 的丢包原因给用户看"。所以这一组填不填没有一条独立的资料来定，只能跟着决定-012 的回答：你在那里选做法六，这一组就要填；选别的，这一组不填。
9. 第 2 到 7 步各组都不填。上游有 8 张回调表是空的。
10. 有没有比各家都好的做法：没有找到。
11. 这个结论在什么情况下是错的：有程序要靠 `devlink dev info` 里的版本认硬件。OpenWrt 上游的镜像上本来就没有版本；读源码排除了。

**去处和理由**

要用户定。丢包原因那一组填不填，和第四步设计第九节决定-012 是同一件事，那个决定还没有回答，这里不能替它定；别的各组不填，各有一条出自资料的理由。

**建议**

回调表全空，端口不带回调表。丢包原因那一组不填，和第四步设计第九节决定-012 的建议（丢的帧放进 `ip -s link` 和 `ethtool -S`）是一起的。

**结论和设计说明**

驱动登记一台 devlink 设备，回调表是空的。`devlink dev show` 列出这台设备（名字是 `platform/` 加 PPE 这个平台设备的名字）；`devlink dev info` 给出驱动的名字；`devlink dev reload`、`devlink dev flash` 回"不支持"，`devlink sb show`、`devlink trap show` 什么都列不出。这台设备的用处是挂五个端口和健康报告。

**你的回答**

> 回调表全空，端口不带回调表

## 决定-010　镜像里默认带不带 `devlink` 这个命令

去处：要用户定。

**资料归类以后的结果**

- 内核怎样规定：devlink 的东西只经 netlink 给用户，内核里没有对应的 sysfs 文件；健康报告出错时内核不往内核记录里写（决定-006）。
- OpenWrt 上游的默认行为：`devlink` 命令是 iproute2 这个包里单独的一个软件包，[references/upstream/openwrt/package/network/utils/iproute2/Makefile 第 107 行](../../../references/upstream/openwrt/package/network/utils/iproute2/Makefile#L107) `define Package/devlink`。OpenWrt 上游这块板子默认的软件包里没有它，[references/upstream/openwrt/target/linux/qualcommax/image/ipq807x.mk 第 120 行](../../../references/upstream/openwrt/target/linux/qualcommax/image/ipq807x.mk#L120) `DEVICE_PACKAGES := ipq-wifi-buffalo_wxr-5950ax12`；qualcommax 的镜像定义和 `include/target.mk` 里搜 `devlink`，零处。所以 OpenWrt 上游的镜像上没有这个命令，用户要自己装。
- 新驱动要靠这个命令的地方：看健康报告的状态和出错次数（决定-005）；手动恢复（决定-007）；诊断和存下的现场（决定-008）；第四步设计第九节决定-001、决定-004 要给用户看的数放在诊断里。
- 不靠这个命令也看得到的：硬件报的每一种错在内核记录里有一行（决定-006）；账对不上的次数在 `ethtool -S` 里（第四步设计第九节决定-009）；自动恢复不用命令（决定-007）。

**每一种做得成的做法**

- 做法一：镜像默认不带，和 OpenWrt 上游一样。
  - 用户看到什么：路由器上没有 `devlink` 命令。出了错只看得到内核记录里的一行和 `ethtool -S` 里的计数；报告的状态、诊断、现场、手动恢复要先装这个软件包。路由器出错时不一定连得上网去装。
- 做法二：这块板子的默认软件包里加上 `devlink`。
  - 要改的东西：这块板子在镜像定义里的默认软件包那一行。
  - 用户看到什么：路由器上有 `devlink` 命令，第五步的东西开箱就能看、能用。镜像多一个软件包。用户给的资料（OpenWrt 官方给这个架构编的 6.18.0-r2 的包，没有在本地核）：`devlink` 的下载包 51848 字节，装上以后 119253 字节；它依赖的 `libmnl0` 下载包 8327 字节，装上以后 20528 字节。压进镜像以后多多少，不能直接拿这几个数当。
- 做法三：不带命令，驱动另外把报告的状态经 sysfs 或者 debugfs 的文件给出来。

**演绎**

1. 这件事要保证一样：第五步做出来的东西（健康报告的状态、诊断、现场、手动恢复），用户在出事的那一刻用得上。
2. 做法三：内核已经有 devlink 这个标准接口，驱动再自己做一套文件，内核的审稿人不会收。否掉。
3. 做法一：命令要先装才有。出事的时候（例如发送都停住了）路由器多半连不上网，装不了；这时候恰好是最需要看诊断和现场的时候。保证不了。它的好处是镜像和 OpenWrt 上游一样、不多一个包。
4. 做法二保证了。代价是要改这块板子的镜像定义，镜像多一个软件包。
5. 把第四步设计第九节决定-003、这里的决定-007 的建议和这一件放在一起看：自动恢复开着的时候，偶尔出一次的错不用命令也能恢复；但是报告一停在"出错"（硬件一直报着，或者两次错隔得比宽限期近），内核就不再自动恢复，哪一种错再来都只是关掉那一位，要用命令才再打开。选做法一的话，这一步只能靠重新加载驱动。两边的建议要一起做才做得成，所以这里建议带。
6. 有没有比各家都好的做法：没有找到。
7. 这个结论在什么情况下是错的：这块板子的存储放不下多出来的这个包。装上以后两个包一共十四万字节上下（用户给的资料）；压进镜像以后多多少，E-0006 编镜像时比一下带和不带的镜像大小，记进报告。

**去处和理由**

要用户定。要改驱动以外的东西（这块板子的默认软件包）；和 OpenWrt 上游的默认行为不一样。

**建议**

做法二。依据是演绎第 3、5 步。

**结论和设计说明**

这块板子的镜像默认装着 `devlink` 命令。用户登上路由器就可以执行 `devlink health show` 看健康报告，`devlink health diagnose` 看此刻的状态，`devlink health dump show` 看出错时存下的现场，`devlink health recover` 手动恢复，`devlink port show` 看五个端口。

**你的回答**

> 做法二

## 函数清单

照各个决定定下来的做法写。新加的函数放在新驱动的文件夹里新的一个文件 `ppe_devlink.c`；报错中断的三个函数在 `edma.c`（第四步设计第九节的函数清单）。

| 文件 | 函数或者定义 | 做什么 | 出自哪个决定 |
|---|---|---|---|
| `ppe_devlink.c` | `ppe_devlink_ops` | devlink 设备的回调表，空的 | 决定-009 |
| `ppe_devlink.c` | `ppe_devlink_alloc` | 加载的最前面调：建 devlink 设备（还不登记），把它和 PPE 的平台设备连起来 | 决定-009 |
| `ppe_devlink.c` | `ppe_devlink_port_register` | 给一个口填端口属性（物理口、口号、"属于哪台交换机"的编号）、登记端口、把端口连到还没有登记的网口上 | 决定-001、决定-002、决定-003、决定-004 |
| `ppe_devlink.c` | `ppe_devlink_port_unregister` | 网口注销以后，注销这个口的端口 | 决定-001 |
| `ppe_devlink.c` | `ppe_health_create`、`ppe_health_destroy` | 建、销毁三个健康报告 `edma_hw`、`edma_ring`、`edma_rxfill` | 决定-005 |
| `ppe_devlink.c` | `edma_hw_reporter_ops` | 回调表：恢复、诊断、存现场；默认宽限期 5000 毫秒，突发期不给（是 0） | 决定-007、决定-008 |
| `ppe_devlink.c` | `edma_ring_reporter_ops`、`edma_rxfill_reporter_ops` | 回调表：诊断、存现场；没有恢复 | 决定-007、决定-008 |
| `ppe_devlink.c` | `edma_hw_recover` | 调 `edma_misc_recover`，把它的返回值交回内核 | 决定-007 |
| `ppe_devlink.c` | 三个报告各自的诊断、存现场的回调 | 把决定-008 演绎第 6 步列的东西写进内核给的消息 | 决定-008 |
| `ppe_devlink.c` | `ppe_health_put_ring`、`ppe_health_put_tx_rings`、`ppe_health_put_rx_rings` | 把一个环读写的位置、主机的各个发包环和发完环、各个空缓冲区环和收包环写进消息；几个诊断和存现场的回调共用 | 决定-008 |
| `ppe.c` | `ppe_probe`、`ppe_remove` | 加载、卸载的次序里加进上面几步，见伪代码 | 决定-001、决定-005、决定-006 |
| `ppe_netdev.c` | `ppe_netdev_register_all`、`ppe_netdev_unregister_all` | 登记每个网口以前先调 `ppe_devlink_port_register`；注销网口以后调 `ppe_devlink_port_unregister` | 决定-001 |
| `edma.c` | `edma_misc_irq`、`edma_fault_work`、`edma_misc_recover` | 见第四步设计第九节的伪代码；工作 `edma_fault_work` 里另外管 `edma_rxfill` 的上报和改回"健康" | 决定-006、决定-007 |
| `edma.c` | `edma_misc_irq_enable` | 把报错中断的九位全打开或者全关掉；加载的最后打开，卸载的开头关掉 | 决定-006 |
| `edma.c` | `edma_ring_fault_note`、`edma_fill_empty_note` | 收包、收"发完"的轮询里调：记下头一次的现场或者立起记号，排工作 | 决定-006 |
| `edma_rx.c` | 收包的轮询 | 空缓冲区环空了：头一次记下是哪个环、排工作；补上以后再排一次工作 | 决定-005、决定-006 |
| 这块板子的镜像定义 | 默认的软件包那一行 | 加上 `devlink` | 决定-010 |

## 伪代码

加载（`ppe_probe`）：

```
建 devlink 设备（不登记）                      内核要求：别的东西都登记好了，最后才登记设备
……原来的步骤：时钟、复位 PPE、开机配置……
建 EDMA 的环；初始化工作 edma_fault_work；申请中断（报错的中断在里面，九位都还关着）
                                              登记了的网口马上能发包，所以环和中断要先在
建三个健康报告 edma_hw、edma_ring、edma_rxfill
对每一个有网口的口（照口号从小到大）：
    填端口属性：种类是物理口；口号是 PPE 里的口号；"属于哪台交换机"的编号是 PPE 的寄存器起始地址
    登记端口，登记用的编号是口号
    把端口连到这个口的网口上                    内核要求：要在登记网口以前
    登记网口
打开报错中断的九位
登记 devlink 设备
立起"可以上报"的记号，排一次工作               内核要求：上报的时候设备要已经登记；在这以前记下的错这时报
```

中间哪一步失败，照相反的次序把已经做了的撤掉。

卸载（`ppe_remove`）：

```
放下"可以上报"的记号；取消工作 edma_fault_work，并等它做完     从这一刻起不再上报，只计数
注销 devlink 设备                              用户的命令从这一刻起进不来
关掉报错中断的九位
对每一个口：注销网口；注销端口
……原来的步骤里：关环的中断、停轮询、释放中断；再取消一次工作并等它做完；停环、释放环……
销毁三个健康报告
关时钟；释放网口
释放 devlink 设备
```

登记一个口的端口（`ppe_devlink_port_register`）：

```
属性清零
属性.种类 = 物理口
属性.口号 = 这个口在 PPE 里的口号
属性."属于哪台交换机"的编号 = PPE 的寄存器起始地址（4 个字节）
把属性交给内核
登记端口（不带回调表），编号 = 口号；失败就返回
把端口连到网口上
```

工作里管 `edma_rxfill` 的那一段（接在第四步设计第九节 `edma_misc_work` 的伪代码后面）：

```
（轮询里：头一次发现一个空缓冲区环空了，立起"空着"和"还没有报"两个记号，记下是哪个环；
  "空着"立着的时候发现这个环又有缓冲了，立起"补上了"的记号。两处都排工作。）
如果"还没有报"的记号立着：
    放下这个记号
    向健康报告 edma_rxfill 上报，带上空的是哪个环
如果"补上了"的记号立着：
    放下这个记号
    叫内核把 edma_rxfill 的状态改回"健康"
    放下"空着"的记号                           下一次再空，再报一次
```

诊断（`edma_hw` 的，用户执行命令时调，可以睡眠）：

```
读状态寄存器、驱动里存着的"还开着的位"
写进消息：状态；还开着的位；九种错各来过几次；没有认得的位来过几次
对每一个主机的发包环、发完环：读硬件里的读写位置，写进消息
```

存现场（`edma_hw` 的，内核在上报时调；用户执行 `devlink health dump show` 而没有存着的现场时也调）：

```
如果是上报时调的：把上报带来的"报了的位""上报时的状态"写进消息
对每一个主机的发包环、发完环：读硬件里的读写位置，写进消息
```

## 上机要看的东西和判据（E-0006 里第五步的部分）

和第四步设计第九节的判据是同一次上机，线、引导的办法都一样。这一部分除了第 6、7、8 条都只是读命令的输出，加起来一刻钟上下。

| 条 | 管哪个决定 | 做什么 | 读什么 | 判据 |
|---|---|---|---|---|
| 1 | 决定-009、决定-010 | 内存引导以后登上路由器 | `devlink dev show`、`devlink dev info`；编镜像时 `devlink` 这个软件包的大小 | 路由器上有 `devlink` 命令；`devlink dev show` 一行，名字以 `platform/` 开头；`devlink dev info` 里有驱动的名字。软件包的大小只报数 |
| 2 | 决定-001、决定-003、决定-004 | 不做别的 | `devlink port show`；五个口的 `/sys/class/net/<口>/phys_port_name`；`ip link` 里网口的名字 | `devlink port show` 五行，种类都是物理口，口号 2 到 6，依次连着 `lan4`、`lan3`、`lan2`、`wan`、`lan1`，没有 `lanes`；物理口名是 p2 到 p6；网口的名字和登记端口以前一样 |
| 3 | 决定-002 | 不做别的；再让主机的 `eno2` 和 USB 网卡经路由器的桥（`lan2`、`lan3`）互相 ping 100 个 | 五个口的 `phys_switch_id`；ping 回了几个 | 五个口读到同一个数，不是空的；ping 回 100 个（填了编号，桥照旧在软件里转发） |
| 4 | 决定-005、决定-007 | 不做别的 | `devlink health show` | 三行：`edma_hw`、`edma_ring`、`edma_rxfill`，状态都是健康，出错次数 0；`edma_hw` 的自动恢复开着、宽限期 5000；另外两个没有恢复 |
| 5 | 决定-008 | 路由器往 `wan` 发着流量的时候 | `devlink health diagnose` 三个报告各一次 | `edma_hw` 的诊断里有状态、还开着的位（0x1ff）、九种错的次数（都是 0）、没有认得的位的次数、每个发包环和发完环的位置；连着读两次，`wan` 的发包环的位置在变 |
| 6 | 决定-005、决定-006、决定-007、决定-008 | 第四步设计第九节判据第 12 条造出"发包的内存满了"以后（实验用的模块） | `devlink health show`；`devlink health dump show`；内核记录里这种错的行数和每一行的时刻；等 10 秒以后执行 `devlink health recover`，再读一次 | 恢复的回调是"先看状态"的写法时：`edma_hw` 是出错，出错次数 1，恢复次数 0，内核记录 1 行。恢复的回调是"不看状态"的写法时：出错次数 2，恢复次数 1，内核记录 2 行；两行之间隔多久只报数，超过 1 秒就把宽限期加长。两种写法都要：存着一份现场，里面有报了的位（0x10）和各个环的位置；`edma_ring`、`edma_rxfill` 还是健康。手动恢复以后硬件还报着：这一位还关着（或者打开以后马上又关上），报告还是出错 |
| 7 | 决定-005、决定-006、决定-007 | 第四步设计第九节判据第 10 条把页的号、"发完"登记的号各改坏一次以后（实验用的模块） | `devlink health show`；`devlink health dump show`；`devlink health recover` 对 `edma_ring` 的回话 | `edma_ring` 是出错，出错次数 1 或者 2；现场里有是哪一样、哪个环、哪个号；`edma_hw` 是健康；对 `edma_ring` 执行恢复回"不支持" |
| 8 | 决定-005、决定-006、决定-007 | 第四步设计第九节判据第 10 条里"第 0 组收了帧不补缓冲"的开关打开，等空缓冲区环空了；再关掉开关（实验用的模块） | 开关开着时和关掉 10 秒以后各读一次 `devlink health show`；`devlink health dump show` | 空着的时候 `edma_rxfill` 是出错；关掉开关以后回到健康，出错次数不是 0；存着一份现场，里面有空的是哪个环；`edma_hw`、`edma_ring` 一直是健康 |
| 9 | 决定-001、决定-005、决定-006 | 把驱动卸载、再加载，三遍（正式的驱动是模块时；编进内核的话这一条换成重新内存引导三遍） | 内核记录里新增的警告；卸载以后的 `devlink dev show`、`devlink port show`、`devlink health show` | 内核记录没有新增的警告；卸载以后三个命令都是空的；再加载以后和第 1、2、4 条一样 |

## E-0006 量到的

读数都是直接在硬件上、在新驱动上量出来的，出处是 [上机实验/E-0006/报告.md](../../上机实验/E-0006/报告.md) 的第 3、4 节。九条判据都通过了。

- 决定-001 到决定-004、决定-009、决定-010：路由器上有 `devlink` 命令；`devlink dev show` 一台设备；`devlink port show` 五行，都是物理口，口号 2 到 6 依次连着 `lan4`、`lan3`、`lan2`、`wan`、`lan1`，没有 `lanes`；物理口名是 p2 到 p6；五个口的 `phys_switch_id` 是同一个数；主机两块网卡经路由器的桥互相 ping 100 个回 100 个。
- 决定-005、决定-008：`devlink health show` 三行，都是健康、出错 0 次，`edma_hw` 自动恢复开着、宽限期 5000。诊断里有状态、还开着的位（0x1ff）、九种错的次数、没有认得的位的次数、14 个发包环和 4 个发完环的位置；流量跑着时连着读两次，`wan` 的发包环的位置在变。
- 决定-006、决定-007（`edma_hw`）：造出"发包的内存满了"以后，`edma_hw` 是出错、出错 1 次、恢复 0 次，内核记录 1 行，存着的现场里报了的位是 0x10、带着各个发包环的位置；`edma_ring`、`edma_rxfill` 还是健康。关着的这一位在状态寄存器里读得到（屏蔽寄存器 0x1ef 时状态是 0x10），往状态寄存器写 0x10 清不掉，所以恢复的回调照决定-007 选的做法二写：先看状态，还报着的位不打开，不往状态寄存器写。手动恢复回"忙"，报告还是出错。
- 决定-007（`edma_ring`、`edma_rxfill`）：页的号、"发完"登记的号各改坏一次，`edma_ring` 是出错、出错 2 次，现场里有是哪一样、哪个环、哪一格，对它执行恢复回"不支持"。0 号收包环不补缓冲的时候 `edma_rxfill` 是出错，补上以后回到健康、出错次数留着，现场里有空的是哪个环。
- 卸载、加载三遍：每一遍卸载以后三个命令都是空的，加载以后 5 个端口、三个报告都是健康；内核记录没有新增的警告。

## 第五步的记录

**回头过了一遍的函数和伪代码**

- 上报要等 devlink 设备登记以后：内核在上报的路上查设备登记了没有（[net/devlink/health.c 第 516 行](../../../references/upstream/linux/net/devlink/health.c#L516) `ASSERT_DEVLINK_REGISTERED(devlink);`，编译用的 6.18.44 里是同一行）。所以工作在设备登记以前、注销以后不上报，这一段里出的错只关位、只计数，登记以后补报。只有一种做法。
- 加载、卸载的次序：devlink 设备最后登记、最先注销，端口在登记网口以前连，是内核的要求（[net/devlink/core.c 第 415 行](../../../references/upstream/linux/net/devlink/core.c#L415) `void devlink_register(struct devlink *devlink)`、[include/linux/netdevice.h 第 2659 行](../../../references/upstream/linux/include/linux/netdevice.h#L2659) `WARN_ON((dev)->reg_state != NETREG_UNINITIALIZED);	\`），只有一种做法。
- 用自己拿锁的那一组函数（[include/net/devlink.h 第 1819 行](../../../references/upstream/linux/include/net/devlink.h#L1819) `int devlink_port_register_with_ops(struct devlink *devlink,`、[include/net/devlink.h 第 2044 行](../../../references/upstream/linux/include/net/devlink.h#L2044) `devlink_health_reporter_create(struct devlink *devlink,`），还是驱动先拿 devlink 的锁再用不拿锁的那一组：路由器的行为一样，是实现上的写法。
- 工作只有一个，三个报告共用：`edma_hw` 的恢复在工作里被内核当场调到时，会去改屏蔽寄存器；这时中断处理函数可能同时进来，用关着中断的锁护着（第四步设计第九节的记录）。只有一种做法。
- 恢复的回调先不先看状态寄存器：路由器的行为不一样（硬件一直报着的错记一行还是两行、报告靠什么停在"出错"）。在决定-007 的做法一、做法二里。
- 突发期给不给：给了，头一次恢复以后的那一段时间里内核每一次都自动恢复，硬件一直报着的错在这一段里会转个不停。不给（是 0）。在决定-007 的资料和函数清单里；它只有"不给"这一种和决定-007 要保证的第二样不冲突的做法。
- 报告的名字 `edma_hw`、`edma_ring`、`edma_rxfill`：用户看得见，是决定-005 定下来的名字。
- "属于哪台交换机"的编号用几个字节：寄存器的起始地址是 4 个字节，照原样填；在决定-002 里。
- 健康报告销毁的时候内核不等正在进行的上报（资料文件决定-005"内核自己怎样规定"），所以卸载时先让中断和轮询都停下、取消工作并等它做完，再销毁报告；写在卸载的伪代码里，只有一种做法。
- 诊断里读每个环的位置时，流量在跑，读到的几个数不是同一刻的：诊断给的是"大致走到哪里"；要同一刻的数得把环停下来，不做。这不改路由器的行为。
- 回头过完，没有找到不在 10 个决定里的"也可以是另一种写法、路由器的行为会不一样"的地方，没有加决定号。

**放在一起核的结果**

第四步设计第九节的 18 个决定和这里的 10 个决定放在一起核的整张表，写在第四步设计第九节的"第五步的记录"里。下面这张表只列这 10 个决定之间的：

| 哪个决定 | 用到了哪个决定的建议 | 照两边一起做，做不做得成 |
|---|---|---|
| 决定-003（只登记有网口的口） | 决定-009（端口不带回调表，也没有共用缓冲那一组回调） | 做得成：没有网口的端口上什么都做不了，所以不登记 |
| 决定-004（不拆口） | 决定-009（端口不带回调表） | 做得成，两处说的是同一件事 |
| 决定-006（在工作里报） | 决定-005（报给哪个报告） | 做得成：决定-005 定了三个报告，工作照故障是哪一类报给哪一个 |
| 决定-007（自动恢复、宽限期、驱动自己改回健康） | 决定-005（三个报告分开） | 做得成：三个报告各有自己的状态，`edma_hw` 的自动恢复不碰另外两个 |
| 决定-008（诊断、存现场） | 决定-005（几个报告）、决定-010（要 `devlink` 命令才读得到） | 做得成：决定-010 定了镜像带 `devlink` 命令，诊断和现场登上路由器就读得到 |
| 决定-007（手动恢复） | 决定-010（要 `devlink` 命令） | 做得成：决定-010 定了镜像带 `devlink` 命令，报告停在"出错"以后用户登上路由器就能再打开关着的位 |
| 决定-009（回调表全空） | 第四步设计第九节决定-012（丢的帧放进 `ip -s link` 和 `ethtool -S`） | 做得成：第四步设计第九节决定-012 定的是做法三加做法四，不经丢包原因，决定-009 里丢包原因那一组不填 |
