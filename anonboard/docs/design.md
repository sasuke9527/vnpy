# anonboard 技术规格

> 目标产品：无需注册的匿名板。同一个人在同一帖子内昵称固定，换帖子昵称不同且读者无法关联；同时具备让产品活下来所需的审核、封禁、追溯与合规能力。
> 本文是"已实现 + 下一步"的规格：标 **[已实现]** 的在本仓库代码中有对应实现和测试；标 **[下一步]** 的是原生客户端与生产部署阶段的工作。背景与法规见 [research.md](research.md)。

## 1. 目标与非目标

**目标**
- R1 无注册：不要手机、邮箱、密码，安装即可发帖（海外档位）。
- R2 帖内一致、跨帖不可关联的化名；楼主有标记。
- R3 尽量不被下架：满足 Apple 1.2 / Google Play UGC 的四项硬要求，满足大陆"后台实名、前台自愿"与日志留存、可追溯要求；能在数小时内下架内容、封禁作恶者。
- 一套代码两种部署档位：`overseas`（无身份绑定）与 `cn`（发帖前后台绑定手机号）。

**非目标**
- 不追求"运营方也无法关联"。法律要求可追溯，任何做到这一点的方案在大陆违法、在海外也过不了传票和 Apple 1.2 的"驱逐违规用户"。我们做的是把关联能力压缩到一个受审计的窄接口。
- v1 不做图片、私信、附近、公司/学校圈、通讯录、搜索、热榜、推荐算法（见第 9 节）。

## 2. 威胁模型

| 对手 | 想做什么 | 我们的承诺 |
| --- | --- | --- |
| 普通读者 / 其他用户 | 把某人在 A 帖的化名和 B 帖的化名对上；知道某个化名是谁；知道谁是楼主以外的"熟人" | 做不到：化名是服务端密钥下的 PRF 输出，公开 API 不含设备标识，时间戳取整到分钟，屏蔽仅限本帖，无个人页、无"TA 的其他帖子"。剩余侧信道：文风、自曝、截图外传。 |
| 拿到内容库备份的人（DBA、外包审核、泄露） | 同上 | v1 原型的单库里 `thread_aliases` 与 `comments` 含 `device_id`，能关联；生产部署把设备表与别名映射拆到独立身份库并加密（第 3、7 节）。 |
| 运营方内部人员 | 私下查某人 | 只能通过 `/admin/trace` 逐条查询，必须填案件编号，每次写入审计日志；管理员身份由令牌决定而非自报。 |
| 作恶用户 | 刷帖、人肉、诽谤、刷举报隐藏别人、逃逸封禁 | 限流、发布前过滤、举报需要多个已建立设备且来自不同 IP、封禁按设备与手机号传播、影子封禁对本人不可见。承认：换硬件即可逃逸。 |
| 平台与监管 | 要求下架内容、定位发布者、检查日志 | 数小时内可处置；`/admin/trace` 返回设备、IP 轨迹、绑定手机号；事件日志保留 ≥ 180 天。 |
| 执法请求 | 调取发布者信息 | 只接受书面文书，一次一条内容，无批量导出，全程审计。 |

## 3. 身份模型

### 3.1 无注册的设备身份 [已实现]

- 首次启动生成 P-256（secp256r1）ECDSA 密钥对。选 P-256 而非 Ed25519：WebCrypto、iOS Secure Enclave、App Attest、Android Keystore、Play Integrity 都只产 P-256，同一套验签代码日后能直接接受硬件证明的密钥。
- `device_id = SHA-256("anonboard:device:v1:" || 65 字节未压缩公钥)`。服务器只存公钥与这个哈希。
- 每个写请求签名，读请求可选签名（签了才有 `is_me`、`my_alias`、自己的屏蔽视图）：

```
X-Device-Key   base64url(公钥, 65 字节)
X-Device-Ts    Unix 秒（≤ 20 位数字；与服务器时间偏差 ≤ 300 s）
X-Device-Nonce 8–64 字符随机串；写请求内 10 分钟去重，读请求不消耗
X-Device-Sig   base64url(r||s, 64 字节)

签名串 = ts "\n" nonce "\n" METHOD "\n" path[?query] "\n" sha256hex(body)
```

服务端对"线上的字节"验签（`raw_path` + `query_string`），不是框架重新解析的 URL；含 `#` 或控制字符的路径直接拒绝。时钟偏差过大的 401 响应带 `server_time`，`/v1/config` 也返回它，客户端据此校正后重试一次。

- 注册 `POST /v1/devices {public_key, terms_version, attestation?}`：必须携带当前版本的社区规范（Apple 1.2 要求对 UGC 明示零容忍条款）；存库的是验签通过的那把公钥的规范编码，不是客户端字符串。

### 3.2 密钥存放与重装行为 [下一步]

| 平台 | 存放 | 卸载重装 | 封禁如何延续 |
| --- | --- | --- | --- |
| iOS | Secure Enclave 生成，Keychain `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`，不同步 iCloud | Keychain 通常保留（Apple 不承诺） | DeviceCheck 两比特：bit0=已封禁，bit1=已注册；抹机也保留 |
| Android（有 GMS） | Keystore（StrongBox 优先） | Keystore 清空 = 新身份 | Play Integrity device recall 位 + 新设备观察期 |
| Android（无 GMS，大陆机型） | Keystore | 新身份 | 手机号（cn 档位）是真正的锚点 |
| Web 原型 | localStorage 存 JWK（演示用；正式应存不可导出的 CryptoKey 于 IndexedDB） | 清站点数据即新身份 | 最低信任层 |

- 注册时附带 App Attest（iOS）/ Play Integrity（Android）证明，服务端据此设置信任层级：已证明设备正常配额，未证明设备配额减半、前几条内容先审后发。
- 封禁写到所有可用锚点：`device:`、`phone:`（cn）、DeviceCheck / recall 位、24 小时的 IP。

### 3.3 大陆档位：后台实名、前台化名 [已实现]

- 触发时机：第一次写操作（发帖、评论、举报）。阅读不需要绑定，这同时满足网络安全法第 24 条（针对"信息发布"）与 Apple 5.1.1(v)。
- 流程：`POST /v1/me/phone/start {phone}` 发 6 位验证码（10 分钟有效，5 次尝试）；`POST /v1/me/phone/verify {code}` 绑定。
- 防探测：start 永远返回 200；"该号码已绑定的设备是否达上限"只在 verify 成功后判断，所以陌生人无法用 start 探测某个号码是否在用；每次 start 都先计入限流再处理；同一号码每小时最多 5 条短信，超出静默丢弃。
- 存储：`identity_bindings.phone_hash = HMAC-SHA256(phone_pepper, E.164)` 用于去重与封禁；`phone_enc = Fernet(号码)` 只供合法调取解密。明文不落库、不展示、不参与化名派生。
- 一个号码最多 3 台设备；封禁同时写 `device:` 与 `phone:` 主体，同号新设备一并受限。
- [下一步] 原生客户端改用运营商一键登录；密钥放 KMS，API 进程只能加密不能解密；对虚拟运营商号段单独风控。

## 4. 帖内化名派生 [已实现]

```
seed  = HMAC-SHA256(alias_pepper, "alias:v1:" + thread_id + ":" + device_id + ":" + counter)
name  = ADJECTIVES[u16be(seed[0:2]) mod 80] + "的" + ANIMALS[u16be(seed[2:4]) mod 80]   # 6400 个名字
hue   = u16be(seed[4:6]) mod 360
```

- **稳定**：同一 (thread, device) 派生结果不变。
- **跨帖独立**：`thread_id` 参与 HMAC，不持有 pepper 无法判断两个名字是否同源。
- **帖内唯一**：首次在某帖写入时，从 `counter=0` 起找第一个未被占用的名字，写入 `thread_aliases(alias_id 随机, thread_id, device_id, name, hue, is_op)`，表上有 `UNIQUE(thread_id, device_id)` 与 `UNIQUE(thread_id, name)`。之后评论只引用 `alias_id`，名字永不重算，pepper 轮换只影响新帖。`counter ≥ 32` 退化为编号名保证终止。
- **楼主**：发帖时分配 `is_op=1` 的化名；楼主在本帖的评论名字相同并带"楼主"徽标；楼主在其他帖（包括他自己发的另一帖）名字完全不同。不给楼主固定名字，否则楼主跨帖可识别。
- **潜水不留痕**：`GET /v1/threads/{id}` 里的 `my_alias` 是预览（`provisional: true`），不写库；只有真的发言才产生 (thread, device) 记录。
- **删除不换名**：删除自己的评论不删化名行，否则删了再发可以换名绕过别人的屏蔽。
- **公开响应永不包含** `device_id`；时间戳取整到分钟；信息流分页游标是基于插入顺序的不透明串，不暴露精确创建时间。

pepper 归属：`ANONBOARD_ALIAS_PEPPER`，原型存文件；生产放 KMS，应用角色只能逐条调用 `GenerateMac`，批量调用告警 [下一步]。可选升级：客户端 VRF（RFC 9381）派生化名，运营方失去离线枚举能力 [下一步，海外优先]。

## 5. 反滥用 [已实现]

| 机制 | 规则 |
| --- | --- |
| 限流（按设备） | 发帖 3/小时，评论 30/小时，举报 20/小时，屏蔽 60/小时；注册后 24 小时内的新设备：发帖 1/小时、评论 10/小时 |
| 限流（按 IP） | 注册 20/小时；写操作合计 120/小时。`X-Forwarded-For` 只在 TCP 对端是配置的可信代理时才采信，取最右侧非代理地址 |
| 发布前过滤 | 手机号、身份证号、银行卡号正则 → 拒绝（反人肉）；`!` 词表 → 拒绝；敏感词表（曝光、爆料、内幕、裁员名单、工资单、小三、开房、身份证号、家庭住址……）→ 先审后发（仅作者可见）；预留分类器钩子接入内容安全服务 |
| 举报 → 自动隐藏 | 每设备对每目标一票；只有注册满 24 小时的设备的票有效；有效票需 ≥ 3 个设备 **且** ≥ 3 个不同 IP（cn 档位再加 ≥ 3 个不同手机号）才自动隐藏；所有举报都进审核队列；不向用户返回举报计数 |
| 状态不可探测 | 对隐藏、待审、影子、已删除的内容，举报与屏蔽接口一律返回与"不存在"相同的 404 |
| 封禁 | `bans(subject, until, reason, shadow)`，主体为 `device:` 或 `phone:`；影子封禁的作者看到自己的内容一切正常（响应里状态映射为 visible），他人不可见 |
| 屏蔽 | 仅限本帖 `(thread_id, alias_id)`，`alias_id="*"` 为"不看此帖"。跨帖屏蔽故意不做：否则屏蔽者用两台设备对比其他帖里谁消失了，就能把化名串起来 |
| 作者自删 | 软删除，正文保留到留存期满后清空 |

[下一步] 举报权重与举报人信誉、strike 阶梯、同帖刷屏冷却、近重复内容检测（SimHash）、注册簇识别、全站只读/全部先审的紧急开关。

## 6. 合规架构

### 6.1 两种部署档位 [已实现]

| | overseas | cn |
| --- | --- | --- |
| 身份 | 设备密钥 | 设备密钥 + 首次写前手机绑定 |
| 日志留存 | 180 天（可缩短） | ≥ 180 天（网络安全法第 21 条） |
| 封禁传播 | 设备 | 设备 + 手机号 |
| 举报有效票 | 设备 + IP | 设备 + IP + 手机号 |

[下一步] 两个档位独立部署、独立数据库、独立密钥、独立运营主体，不共享任何用户表；cn 构建的功能差异由服务端决定，不能靠客户端判断。

### 6.2 日志与留存 [已实现]

- `events(ts, device_id, ip, action, target_id, meta)` 记录所有写操作与注册；`POST /admin/maintenance/purge` 删除超过 `retention_days` 的事件、过期 nonce/验证码/封禁，并清空留存期满的已删除内容正文；每次清理写审计日志。
- [下一步] 访问日志加密写对象存储（WORM），身份库单独备份。

### 6.3 合法调取 [已实现]

- `GET /admin/trace/{thread|comment}/{id}?case_ref=…&reason=…`：案件编号与事由必填；返回内容、设备（公钥、注册时间、首见 IP、证明信息）、最近 200 条事件（时间、IP、动作）、绑定手机号（cn）。一次一条，没有批量导出。
- 每次调取写 `audit_log(ts, admin, action, target, case_ref, reason, result)`；`admin` 由匹配的令牌决定（`ANONBOARD_ADMIN_TOKENS="alice:tok1,bob:tok2"`），客户端自报的名字不采信。
- [下一步] 两人审批、审计日志哈希链并每日锚定到独立存储、手机号解密移到离线工具。

### 6.4 审核流水线 [已实现]

提交 → 归一化与长度检查 → PII 正则 / 拒绝词 / 分类器钩子 / 敏感词 → `visible | held | 拒绝` → 发布 → 举报聚合 → `hidden` → `/admin/queue?status=hidden|held` → 管理员 `restore | hide | delete` → 封禁 / 影子封禁。

[下一步] 申诉接口、SLA 与值班告警、外部文本审核服务。

### 6.5 App Store 审核指南 1.2 清单

- [x] 过滤：发布前规则 + 先审后发 + 分类器钩子
- [x] 举报：每条内容可举报；自动隐藏 + 人工队列
- [x] 屏蔽：帖内屏蔽 + 不看此帖
- [x] 联系方式：`/v1/config.contact_email`，页脚展示
- [x] 条款：注册必须携带当前 `terms_version`
- [x] 5.1.1(v)：无强制注册；cn 档位手机绑定只在首次写时触发并说明法规依据
- [ ] [下一步] 年龄分级 17+，隐私标签（设备标识、用户内容、cn 的电话号码），审核备注附后台截图与 24 小时处理承诺，商店文案不用"匿名/爆料"

### 6.6 大陆上架前置事项 [下一步]

公司主体 → 域名 ICP 备案 → 工信部 App 备案（App Store Connect 填备案号）→ 公安联网备案 → 增值电信业务经营许可证 → 具有舆论属性服务的安全评估 → 持牌内容审核与短信实名接入 → 审核人力与应急预案（一键只读、下架流程、联系人）。

## 7. 数据模型 [已实现]

```
devices(device_id PK, public_key, created_at, first_ip, last_seen_at, status, terms_version, attestation)
identity_bindings(device_id PK, phone_hash, phone_enc, verified_at)            -- cn
otp_challenges(device_id PK, phone_hash, phone_enc, code_hash, expires_at, attempts)
threads(thread_id PK, device_id, body, created_at, status, comment_count, report_count)
comments(comment_id PK, thread_id, device_id, alias_id, body, reply_to, created_at, status, report_count)
thread_aliases(alias_id PK, thread_id, device_id, name, hue, is_op, created_at,
               UNIQUE(thread_id, device_id), UNIQUE(thread_id, name))
reports(target_type, target_id, device_id, reason, created_at, ip, phone_hash, eligible, PK(target_type, target_id, device_id))
blocks(device_id, thread_id, alias_id, created_at, PK(device_id, thread_id, alias_id))
bans(subject PK, until, reason, shadow, created_at)
events(id, ts, device_id, ip, action, target_id, meta)
audit_log(id, ts, admin, action, target, case_ref, reason, result)
nonces(device_id, nonce, ts, PK(device_id, nonce))
```

状态：`visible | hidden（举报或管理员隐藏，待审）| held（敏感词先审后发）| shadow（作者被影子封禁）| deleted`。

[下一步] 拆分为 `content`（无 device_id）、`identity`（设备、封禁、别名映射）、`vault`（手机号、调取记录）三个库/角色；公开读路径只走不含 `device_id` 的视图；审计日志哈希链。

## 8. API [已实现]

| 方法 | 路径 | 签名 | 说明 |
| --- | --- | --- | --- |
| GET | `/v1/config` | 否 | 档位、条款版本、联系邮箱、`server_time`、长度限制 |
| POST | `/v1/devices` | 是 | 注册（幂等） |
| GET | `/v1/me` | 是 | 是否已绑手机、能否发言、封禁截止（影子封禁不显示） |
| GET | `/v1/threads?cursor=&limit=` | 可选 | 信息流，不透明游标 |
| POST | `/v1/threads` | 是 | 发帖 |
| GET | `/v1/threads/{id}` | 可选 | 帖子、评论、`my_alias` 预览、`blocked_alias_ids` |
| POST | `/v1/threads/{id}/comments` | 是 | 回复，可 `reply_to` |
| DELETE | `/v1/threads/{id}`、`/v1/threads/{id}/comments/{cid}` | 是 | 删除自己的内容 |
| POST | `/v1/reports` | 是 | 举报；返回 `{ok, auto_hidden}` |
| POST / DELETE | `/v1/threads/{id}/blocks[/{alias_id}]` | 是 | 屏蔽 / 取消 |
| POST | `/v1/me/phone/start`、`/verify` | 是 | cn 手机绑定 |
| GET | `/admin/queue?status=` | 管理员 | 审核队列 |
| POST | `/admin/content/{kind}/{id}` | 管理员 | `restore | hide | delete` |
| POST | `/admin/devices/{id}/ban`、`/unban` | 管理员 | 封禁（含 shadow、include_phone） |
| GET | `/admin/trace/{kind}/{id}?case_ref=&reason=` | 管理员 | 合法调取 |
| GET | `/admin/audit` | 管理员 | 审计日志 |
| POST | `/admin/maintenance/purge` | 管理员 | 留存清理 |

错误统一 `{"error", "message", ...}`；限流 429 带 `Retry-After`；签名时钟问题的 401 带 `server_time`。

## 9. 不该做的功能

每一项都对应无秘、Secret、脉脉职言或知乎匿名出事的具体形态：

1. 通讯录导入、"朋友的秘密"、好友发现：匿名 + 熟人图 = 关于可辨认个人的八卦与诽谤；也是无秘的增长引擎和死因。
2. 附近 / LBS：位置 + 匿名 = 对本地具体人和商家的攻击。
3. 公司 / 学校 / 行业圈子、邮箱域名验证：脉脉职言被约谈、被企业起诉的形态。
4. 图片 / 视频（v1）：截图是人肉主载体，审核成本最高。
5. 私信 / 群聊：骚扰主渠道，大陆另需即时通信合规。
6. 个人主页、"TA 的其他帖子"、跨帖屏蔽、全局固定昵称、"同一人"标记：直接违反 R2。
7. 全文搜索、按人名/公司名搜索：把树洞变成人肉检索器。
8. 点赞 / 热榜 / 推荐算法：奖励"越狠越热"的爆料；大陆推荐算法还要备案。
9. 阅后即焚、端到端加密、"运营方也无法追踪"的宣传：与留存与追溯义务冲突，是监管眼中的红旗，也是对用户的误导。
10. 第三方广告 / 统计 SDK 采集 IDFA、IMEI、OAID：在我们之外重建设备与行为的链接，也是工信部通报重灾区。

## 10. 实现状态与下一步

**本仓库已完成**：第 3.1、3.3、4、5、6.1–6.4、7、8 节标为已实现的全部内容；47 个单元/集成测试与 1 个 Chromium 端到端测试（WebCrypto 签名与 Python 验签互通、帖内同名跨帖异名、楼主标记）。

**下一步（按优先级）**
1. 原生客户端：Keychain / Keystore 存私钥，App Attest / Play Integrity 证明，DeviceCheck 位。
2. 数据分区：身份库与内容库分离，公开读路径走视图；pepper 与手机号密钥进 KMS。
3. 审核运营：外部文本审核服务、申诉、SLA 告警、举报人信誉、strike 阶梯、紧急只读开关。
4. 审计：哈希链审计日志、两人审批的调取流程、离线解密工具。
5. 海外档位可选：客户端 VRF 化名派生。
6. 大陆上架：主体、备案、许可、安全评估。
