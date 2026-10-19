# 逆向细节：SQLCipher / WAL / V2 图片

> 这一页是给想改代码的人看的。照着 README 跑的话不需要读。

## 1. 数据库：SQLCipher 4

微信 4.x 的 `*.db` 是 SQLCipher 4，参数：

| 参数 | 值 |
|---|---|
| 页大小 | 4096 |
| reserve | 80 |
| IV | 每页最后 80 字节的前 16 字节（偏移 4016） |
| HMAC | 每页最后 64 字节（偏移 4032） |
| KDF | PBKDF2-HMAC-SHA512，密钥已在内存里派生好 |

### 解密一页

```python
PAGE, RESERVE = 4096, 80
IV_OFF = PAGE - RESERVE  # 4016

def dec_page(page, key, pgno):
    iv = page[IV_OFF:IV_OFF + 16]
    if pgno == 1:
        # 第 1 页前 16 字节被 salt 占掉，解密后要补回 SQLite 头
        d = AES.new(key, AES.MODE_CBC, iv).decrypt(page[16:IV_OFF])
        return b"SQLite format 3\x00" + d + b"\x00" * RESERVE
    d = AES.new(key, AES.MODE_CBC, iv).decrypt(page[:IV_OFF])
    return d + b"\x00" * RESERVE
```

### 校验密钥（HMAC）

不要用"解密后能不能看到表头"判断密钥对不对，用 HMAC：

```python
mac_key = PBKDF2_SHA512(enc_key, salt XOR 0x3a, iterations=2)
HMAC_SHA512(mac_key, page1[16:4032] + uint32le(1)) == page1[4032:4096]
```

### 密钥来源

进程内存里有 `x'<64 hex enc_key><32 hex salt>'` 字符串。`salt` 就是文件前 16 字节的 hex。
`dump-keys.ps1` 扫描所有微信进程的内存，按这个模式抓取，再用上面的 HMAC 校验。

> 4.1.10 起这个字符串不再出现在内存里，所以必须锁 4.1.9。

## 2. WAL

微信用 WAL 模式，新消息先写 `message_0.db-wal`。

- WAL 头 32 字节；之后每帧 = 24 字节帧头 + 4096 字节页。
- 帧头：`[0:4]` = 页号（大端），`[8:16]` = salt。
- **只应用 salt 等于 WAL 头 `[16:24]` 的帧**，否则旧帧会把页面覆盖坏。
- WAL 是预分配的（常见 4MB），**size 不变**，所以轮询要用 `mtime` 判断。

合并逻辑：先把主库所有页解密成 `pages[pgno]`，再按顺序用 WAL 帧覆盖，最后按页号拼回一个完整 SQLite 文件。

## 3. 消息表

- 每个会话一张表：`Msg_<md5(对方 wxid)>`。
- `Name2Id(rowid → user_name)`：`real_sender_id` 指向它，用来判断是谁发的。
- `local_id`：会话内单调递增，适合做游标。
- `local_type & 0xFFFF`：1 文本、3 图片、34 语音、43 视频、49 富媒体/文件、10000 系统。
- `WCDB_CT_message_content`：4 表示 `message_content` 是 zstd 压缩的。

## 4. 图片：V2 `.dat`

路径：
```
<微信数据>\msg\attach\<md5(对方wxid)>\<YYYY-MM>\Img\<文件哈希>.dat
```

### 结构

```
[6B  magic] 07 08 56 32 08 07
[4B  aes_size]  little-endian
[4B  xor_size]  little-endian
[1B  pad]       0x01
[aligned_aes_size 字节]  AES-128-ECB
[raw 字节]              不加密
[xor_size 字节]         单字节 XOR
```

`aligned_aes_size`：`aes_size % 16 != 0` 时向上取整到 16；**能被 16 整除时再加 16**（PKCS7 整块 padding）。

解密后用文件头判断格式（`FF D8 FF` = JPEG），并用文件尾校验（JPEG 必须 `FF D9`，PNG 末尾 12 字节含 `IEND`）。

### 密钥（重点）

**不要扫内存**。微信用账号的 `uin` 派生：

```python
xor_key = uin & 0xFF
aes_key = md5(str(uin) + wxid).hexdigest()[:16]   # 16 个 ASCII 字符
```

`uin` 从 kvcomm 缓存文件名取：

```
%APPDATA%\Tencent\xwechat\net\kvcomm\key_<uin>_*.statistic
```

一个账号可能有多个候选（例如 `0` 和真实 uin），逐个用 V2 模板试：AES-ECB 解第一块出现图片 magic 就命中。为防偶合，要求整份文件解出来且尾部合法。

> Windows 上这套公式成立；macOS 上同一原理，但 kvcomm 路径不同。
> 换号/重登后 uin 可能变，重新推导即可。

### 消息 → 文件

XML 里的 `<img md5="...">` **不等于**磁盘文件名。要用 `message_resource.db`：

- `ChatName2Id(user_name → rowid)` 拿到 `chat_id`
- `MessageResourceInfo(chat_id, message_local_id)` → `packed_info`
- `packed_info` 里 `\x12\x22\x0a\x20` 后面 32 字节 ASCII hex 就是文件名哈希

## 5. 发送侧（UI 自动化）

1. `EnumWindows` 找主窗口：class `Qt51514QWindowIcon`，标题非空且不是 `Weixin`。
2. `ShowWindow(SW_RESTORE)` + `SW_SHOW`。
3. 循环 12 次：`SetForegroundWindow`；每次校验 `GetForegroundWindow() == hwnd`；失败就 `AttachThreadInput` 再试。
4. `Ctrl+F` → 剪贴板粘贴备注名 → `Enter`。
5. 内容放剪贴板 → `Ctrl+V` → `Enter`。
6. 回读数据库，确认该会话多了一条自己的消息；没有就重试。

## 6. 常用排查 SQL

```sql
-- 最近的消息（解密后）
SELECT local_id, create_time, real_sender_id, local_type, message_content
FROM Msg_<md5> ORDER BY local_id DESC LIMIT 20;

-- 图片映射
SELECT message_local_id, message_local_type, packed_info
FROM MessageResourceInfo WHERE chat_id = ? ORDER BY message_create_time DESC;
```
