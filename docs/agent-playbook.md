# Agent 复现手册（低 token 版）

把这个文件直接交给你的 agent。目标是：**按顺序执行、每步验收、不要重新逆向**。

## 前提

- Windows 10/11，微信 **4.1.9.57** 已登录小号
- Python 3.10+（`pip install pycryptodome zstandard`）
- Node.js + 你的 agent CLI（支持 `--session-id` 唤醒）
- 仓库已解压到 `C:\wxbridge`

## 执行顺序

```powershell
# 0. 版本检查（必须 4.1.9.x）
(Get-Item "D:\wechat\Weixin\Weixin.exe").VersionInfo.FileVersion

# 1. 取密钥（微信保持登录）
powershell -ExecutionPolicy Bypass -File tools\dump-keys.ps1 > tmp\keys.txt
# 验收：keys.txt 有十几行 salt:key

# 2. 读消息
python tools\wx-read.py 20
# 验收：打印最近 20 条中文消息

# 3. 发消息（先发给自己的另一个号验证）
powershell -ExecutionPolicy Bypass -File tools\wx-ui-send.ps1 -To "备注名" -Text "测试"
# 验收：对方真的收到

# 4. 配置
copy tools\wx-bridge-config.example.json tools\wx-bridge-config.json
# 改 workspace / node / dshBin / 自己的 wxid

# 5. 加联系人（每个联系人一个独立会话）
python tools\wx-bridge.py --list-contacts
python tools\wx-bridge.py --add-peer "备注名"
# 把"主人"那条的 kind 改成 owner

# 6. 自检
python tools\wx-bridge.py --test "在吗"
# 验收：自己收到回复

# 7. 常驻
powershell -ExecutionPolicy Bypass -File tools\wx-bridge-control.ps1
# 验收：tmp\bridge.log 出现「桥接启动」
```

## 规则

1. **只改 4 个 `CHANGE_ME`**（见 README §4），不要动加密参数、WAL、UI 自动化。
2. 失败先查 README §6 对应编号，不要自己发明解密方式。
3. 图片密钥用 `uin + wxid` 公式，不要扫内存。
4. 发送成功必须回读数据库确认，不要信脚本的「已发送」。
5. 一条消息一个 `dsh headless --session-id`；串行，不并发。
6. 用 `--test` 单条验证；不要用定时轮询（贵 10 倍以上）。

## 每步验收失败的常见原因

| 步骤 | 失败 | 看 |
|---|---|---|
| 取密钥 | 0 行 | 微信没登录 / 版本 >4.1.9 |
| 读消息 | 没有密钥 | 重跑 dump-keys；salt 对不上 |
| 发消息 | 没收到 | 窗口托盘/锁屏/最小化；前台锁 |
| 桥接 | 不回复 | 联系人不在 peers；暂停标记；游标 |
| 图片 | 解不开 | .dat 未落盘；uin 变了 |
