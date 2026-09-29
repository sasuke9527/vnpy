# anonboard —— 无需注册的匿名板（原型）

> 同一个人在同一个帖子里始终显示同一个昵称；换一个帖子就是另一个昵称；读者无法把两个昵称对应到同一个人。
> 不需要手机号、邮箱或密码，安装即用。

这是一个可运行的后端 + 网页端原型，用来验证"帖内同名、跨帖异名"的身份设计，
以及一套面向"尽量不被下架"的合规骨架（举报/屏蔽/审核队列/日志留存/合法调取审计/大陆后台实名档位）。
设计背景与法规分析见 [`docs/research.md`](docs/research.md)，技术规格见 [`docs/design.md`](docs/design.md)。

## 快速开始

```bash
cd anonboard
pip install -r requirements.txt
python -m anonboard            # http://127.0.0.1:8080
python -m pytest -q tests      # 42 个单元/集成测试 + 1 个 Chromium 端到端测试（需要 playwright）
```

环境变量（都有默认值，生产必须显式设置）：

| 变量 | 含义 |
| --- | --- |
| `ANONBOARD_PROFILE` | `overseas`（默认，无任何身份绑定）或 `cn`（发言前需在后台绑定手机号，前台仍匿名） |
| `ANONBOARD_DB` | SQLite 路径，默认 `anonboard.db` |
| `ANONBOARD_ALIAS_PEPPER` | 32 字节 hex，派生帖内昵称的服务端密钥（未设置时生成到 `*.alias.pepper` 文件） |
| `ANONBOARD_PHONE_PEPPER` / `ANONBOARD_PHONE_ENC_KEY` | 手机号哈希与加密密钥（`cn` 档位） |
| `ANONBOARD_ADMIN_TOKEN` | 管理接口令牌，请求头 `X-Admin-Token` |
| `ANONBOARD_CONTACT_EMAIL` | 展示给用户的举报/申诉邮箱（App Store 1.2 要求） |
| `ANONBOARD_RETENTION_DAYS` | 日志留存天数，默认 180（网络安全法第 21 条：不少于六个月） |
| `ANONBOARD_REPORT_HIDE_THRESHOLD` | 多少个不同设备举报后自动隐藏并进入人工审核，默认 3 |
| `ANONBOARD_BLOCKLIST` | 词表文件：每行一个词，`!` 开头为直接拒绝，否则先审后发 |

## 工作原理

### 1. 没有账号，只有设备密钥

首次启动时客户端生成一对 P-256 密钥，私钥只存在设备的安全存储里（iOS Keychain / Android Keystore；网页原型用 localStorage）。
每个写请求都带签名头：

```
X-Device-Key   base64url(65 字节未压缩公钥)
X-Device-Ts    Unix 秒
X-Device-Nonce 随机数（服务端 10 分钟内拒绝重复）
X-Device-Sig   base64url(r||s)   对 "ts\nnonce\nMETHOD\npath?query\nsha256(body)" 的 ECDSA-SHA256 签名
```

服务端只保存 `device_id = SHA-256("anonboard:device:v1:" || 公钥)`。没有手机号、邮箱、密码，也没有可找回的账号——
换机或卸载后就是一个新的人（这正是无秘那类产品的用户预期）。

### 2. 帖内昵称派生（`anonboard/pseudonym.py`）

```
seed = HMAC-SHA256(server_pepper, "alias:v1:" + thread_id + ":" + device_id + ":" + counter)
name = 形容词[seed[0:2]] + "的" + 动物[seed[2:4]]      # 例如 "沉默的企鹅"
hue  = seed[4:6] mod 360                                # 头像颜色
```

* 同一设备在同一帖子里派生结果不变 → 昵称稳定；
* `thread_id` 参与 HMAC → 换帖子就是无关的昵称；没有 pepper 的人无法验证两个昵称是否同源；
* 帖内重名时 `counter` 递增换一个候选，选定后写入 `thread_aliases` 表，此后永不改变；
* 楼主额外带"楼主"标记（自己看到的自己带"我"标记）；
* 只有真的发言才会写入 (thread, device, name) 记录；只浏览不会留下关联。

运营方（持有数据库和 pepper）**能**把一个设备的所有昵称关联起来——这是刻意保留的：没有它就无法封禁、限流、响应合法调取。
`docs/design.md` 讲了如何把这张表和 pepper 隔离、审计。

### 3. 屏蔽为什么只作用于当前帖子

如果"屏蔽某人"跨帖子生效，屏蔽者只要观察其他帖子里哪些评论消失了，就能把这个人在各帖子里的昵称串起来。
所以屏蔽只在本帖生效，另外提供"不看此帖"。这是一个有意的取舍，不是遗漏。

### 4. 两种部署档位，一套代码

| | `overseas` | `cn` |
| --- | --- | --- |
| 发言前提 | 注册设备 + 同意社区规范 | 同左，且必须绑定手机号（短信验证码） |
| 手机号存储 | 无 | `HMAC(pepper, 号码)` 用于去重/封禁 + `Fernet` 加密密文用于合法调取；明文不落库、不展示 |
| 封禁传播 | 按设备 | 按设备 + 按手机号（同号新设备一并受限） |

### 5. 反滥用与合规骨架

* 限流：按设备、按 IP、新设备（24 小时内）更严；
* 内容前置检查：手机号/身份证/银行卡号直接拒绝（反人肉），敏感词先审后发（仅作者可见）；
* 举报：3 个不同设备举报即自动隐藏并进入 `/admin/queue`，管理员可恢复/删除；
* 封禁与影子封禁；作者可删除自己的内容；
* 日志：所有写操作记录 `(时间, 设备, IP, 动作)`，`/admin/maintenance/purge` 按留存期清理；
* 合法调取：`/admin/trace/{thread|comment}/{id}?case_ref=…&reason=…` 必须填案件编号，每次调取写入 `audit_log`；
* 时间戳对外只精确到分钟，减少跨帖时间关联。

## API 一览

| 方法 | 路径 | 签名 | 说明 |
| --- | --- | --- | --- |
| GET | `/v1/config` | 否 | 档位、条款版本、联系邮箱、限制 |
| POST | `/v1/devices` | 是 | 注册设备（`public_key`, `terms_version`, 可选 `attestation`） |
| GET | `/v1/me` | 是 | 设备状态：是否已绑手机、能否发言、封禁截止 |
| GET | `/v1/threads?before=&limit=` | 可选 | 信息流；签名时会隐藏你"不看"的帖子并显示你自己的待审内容 |
| POST | `/v1/threads` | 是 | 发帖 `{body}` |
| GET | `/v1/threads/{id}` | 可选 | 帖子 + 评论；签名时含 `my_alias`（你在本帖的昵称预览）与 `is_me` |
| POST | `/v1/threads/{id}/comments` | 是 | 回复 `{body, reply_to?}` |
| DELETE | `/v1/threads/{id}` / `.../comments/{cid}` | 是 | 删除自己的内容 |
| POST | `/v1/reports` | 是 | 举报 `{target_type, target_id, reason}` |
| POST | `/v1/threads/{id}/blocks` | 是 | 屏蔽 `{alias_id}`；`"*"` 表示不看此帖 |
| POST | `/v1/me/phone/start` / `verify` | 是 | `cn` 档位的手机绑定 |
| GET/POST | `/admin/...` | `X-Admin-Token` | 审核队列、内容处置、封禁、合法调取、审计、留存清理 |

错误统一为 `{"error": code, "message": 文案, ...}`，限流返回 429 与 `Retry-After`。

## 做原生 App 时要补的事

* 私钥放 Keychain（`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`，不进 iCloud）/ Android Keystore；
* 注册时附带 App Attest / Play Integrity 的证明，服务端据此给"已证明设备"更高配额，给模拟器/越狱设备更低配额；
* 首次启动强制展示社区规范（对 UGC 零容忍条款）并记录 `terms_version`；
* 举报入口、屏蔽入口、联系邮箱必须在界面上可见（App Store 审核 1.2）；
* 大陆上架前：公司主体、ICP 备案 + App 备案、增值电信许可、接入短信实名、7×24 审核人力。

## 局限

* SQLite + 进程内锁，单进程原型；换 Postgres 只需替换 `db.py`。
* 关键词过滤只是占位，正式产品需要接入文本审核服务。
* 短信通道是桩实现（`DevSmsProvider`），需接入真实短信服务。
* 网页端使用 localStorage 存私钥，仅用于演示；正式客户端必须用系统安全存储。
