# wxkey.ps1 — 从「微信 4.1.9 及更早版本」的进程内存里取出数据库密钥
#
# 用法（微信已登录时运行）：
#   powershell -ExecutionPolicy Bypass -File wxkey.ps1
#   powershell -ExecutionPolicy Bypass -File wxkey.ps1 -SaveKey     # 额外存一份到桌面
#
# 只读内存：不注入、不挂钩、不改微信、不联网。默认不写任何文件。

[CmdletBinding()]
param(
    [string]$DbPath,
    [int]$ProcessId = 0,
    [switch]$SaveKey,
    [switch]$NoPause,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'

$cs = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class WxKey
{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool ReadProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, IntPtr dwSize, out IntPtr lpNumberOfBytesRead);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr VirtualQueryEx(IntPtr hProcess, IntPtr lpAddress, out MEMORY_BASIC_INFORMATION mbi, IntPtr dwLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);

    [StructLayout(LayoutKind.Sequential)]
    public struct MEMORY_BASIC_INFORMATION
    {
        public IntPtr BaseAddress;
        public IntPtr AllocationBase;
        public uint AllocationProtect;
        public IntPtr RegionSize;
        public uint State;
        public uint Protect;
        public uint Type;
    }

    const uint PROCESS_QUERY_INFORMATION = 0x0400;
    const uint PROCESS_VM_READ = 0x0010;
    const uint MEM_COMMIT = 0x1000;

    public static long LastRegions;
    public static long LastBytes;

    static bool Readable(uint p)
    {
        if ((p & 0x100) != 0) return false;
        if ((p & 0xFF) == 0x01) return false;
        uint b = p & 0xFF;
        return b == 0x02 || b == 0x04 || b == 0x08 || b == 0x20 || b == 0x40 || b == 0x80;
    }

    static bool IsHex(byte c)
    {
        return (c >= 0x30 && c <= 0x39) || (c >= 0x41 && c <= 0x46) || (c >= 0x61 && c <= 0x66);
    }

    // 返回引号里完整的十六进制串（通常是 64 位密钥 + 32 位 salt）
    public static List<string> FindInBuffer(byte[] b, int len)
    {
        List<string> found = new List<string>();
        for (int i = 0; i + 66 < len; i++)
        {
            if (b[i] != 0x78 || b[i + 1] != 0x27) continue;   // x'
            int p = i + 2;
            int n = 0;
            while (p < len && IsHex(b[p])) { p++; n++; }
            if (n < 64) continue;
            if (p >= len || b[p] != 0x27) continue;
            char[] hex = new char[n];
            for (int k = 0; k < n; k++) hex[k] = (char)b[i + 2 + k];
            found.Add(new string(hex));
        }
        // UTF-16LE 形态
        for (int i = 0; i + 200 < len; i++)
        {
            if (b[i] != 0x78 || b[i + 1] != 0x00 || b[i + 2] != 0x27 || b[i + 3] != 0x00) continue;
            int q = i + 4;
            List<char> hex = new List<char>();
            while (q + 1 < len && b[q + 1] == 0x00 && IsHex(b[q])) { hex.Add((char)b[q]); q += 2; }
            if (hex.Count < 64) continue;
            if (q + 1 >= len || b[q] != 0x27 || b[q + 1] != 0x00) continue;
            found.Add(new string(hex.ToArray()));
        }
        return found;
    }

    public static List<string> Scan(int pid, out string err)
    {
        err = null;
        LastRegions = 0;
        LastBytes = 0;
        List<string> hits = new List<string>();
        HashSet<string> seen = new HashSet<string>();
        IntPtr h = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, pid);
        if (h == IntPtr.Zero)
        {
            err = "无法打开进程 pid=" + pid + "，Win32 错误 " + Marshal.GetLastWin32Error();
            return hits;
        }
        try
        {
            long addr = 0;
            long max = 0x7FFFFFFF0000L;
            int chunk = 8 * 1024 * 1024;
            byte[] buf = new byte[chunk + 256];
            while (addr < max)
            {
                MEMORY_BASIC_INFORMATION mbi;
                IntPtr q = VirtualQueryEx(h, (IntPtr)addr, out mbi, (IntPtr)Marshal.SizeOf(typeof(MEMORY_BASIC_INFORMATION)));
                if (q == IntPtr.Zero) break;
                long baseAddr = (long)mbi.BaseAddress;
                long size = (long)mbi.RegionSize;
                LastRegions++;
                if (size <= 0) break;
                if (mbi.State == MEM_COMMIT && Readable(mbi.Protect))
                {
                    long pos = 0;
                    while (pos < size)
                    {
                        int toRead = (int)Math.Min((long)chunk, size - pos);
                        if (toRead <= 0) break;
                        IntPtr got;
                        if (ReadProcessMemory(h, (IntPtr)(baseAddr + pos), buf, (IntPtr)toRead, out got))
                        {
                            int n = (int)got;
                            LastBytes += n;
                            List<string> part = FindInBuffer(buf, n);
                            for (int i = 0; i < part.Count; i++)
                                if (seen.Add(part[i])) hits.Add(part[i]);
                        }
                        pos += toRead;
                    }
                }
                long next = baseAddr + size;
                addr = (next > addr) ? next : addr + 0x1000;
            }
        }
        finally { CloseHandle(h); }
        return hits;
    }
}
'@

Add-Type -TypeDefinition $cs -Language CSharp

function ConvertFrom-Hex([string]$hex) {
    $bytes = New-Object byte[] ($hex.Length / 2)
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        $bytes[$i] = [Convert]::ToByte($hex.Substring($i * 2, 2), 16)
    }
    return ,$bytes
}

function Read-Page1([string]$dbFile, [ref]$saltOut) {
    $page = New-Object byte[] 4096
    $n = 0
    try {
        $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        $fs = New-Object IO.FileStream($dbFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
        try { $n = $fs.Read($page, 0, 4096) } finally { $fs.Close() }
    } catch { return $null }
    if ($n -lt 1024) { return $null }
    $salt = New-Object byte[] 16
    [Array]::Copy($page, 0, $salt, 0, 16)
    $saltOut.Value = $salt
    return ,@($page, $n)
}

function Test-Layout([byte[]]$key, [byte[]]$page, [int]$n, [int]$pageSize, [int]$reserve) {
    if ($n -lt $pageSize) { return $false }
    $ivOff = $pageSize - $reserve
    $ctLen = $ivOff - 16
    if ($ctLen -le 0 -or ($ctLen % 16) -ne 0) { return $false }
    $iv = New-Object byte[] 16
    [Array]::Copy($page, $ivOff, $iv, 0, 16)
    $ct = New-Object byte[] $ctLen
    [Array]::Copy($page, 16, $ct, 0, $ctLen)
    try {
        $aes = [System.Security.Cryptography.Aes]::Create()
        $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [System.Security.Cryptography.PaddingMode]::None
        $aes.Key = $key
        $aes.IV = $iv
        $plain = $aes.CreateDecryptor().TransformFinalBlock($ct, 0, $ct.Length)
        $head = [Text.Encoding]::ASCII.GetString($plain, 0, 15)
        return ($head -eq 'SQLite format 3')
    } catch { return $false }
}

$Layouts = @(@(4096, 80), @(4096, 48), @(1024, 48), @(1024, 80))

# 试着用某个候选解库。Deep 模式再试 PBKDF2 派生。
function Test-Key([string]$hexFull, [string]$dbFile, [switch]$Deep) {
    $saltRef = $null
    $r = Read-Page1 $dbFile ([ref]$saltRef)
    if (-not $r) { return $false }
    $page = $r[0]; $n = $r[1]

    if ($hexFull.Length -lt 64) { return $false }
    $keyHex = $hexFull.Substring(0, 64)
    $raw32 = ConvertFrom-Hex $keyHex
    $raw16 = ConvertFrom-Hex $keyHex.Substring(0, 32)

    foreach ($lay in $Layouts) {
        if (Test-Layout $raw32 $page $n $lay[0] $lay[1]) { return $true }
        if (Test-Layout $raw16 $page $n $lay[0] $lay[1]) { return $true }
    }

    if ($Deep) {
        $salt = $saltRef
        $pass = [Text.Encoding]::ASCII.GetBytes($keyHex)
        foreach ($iter in @(256000, 64000)) {
            try {
                $d = New-Object System.Security.Cryptography.Rfc2898DeriveBytes($pass, $salt, $iter, [System.Security.Cryptography.HashAlgorithmName]::SHA512)
                $k = $d.GetBytes(32)
                foreach ($lay in $Layouts) {
                    if (Test-Layout $k $page $n $lay[0] $lay[1]) { return $true }
                }
            } catch { }
        }
    }
    return $false
}

function Test-CanRead([string]$dbFile) {
    try {
        $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        $fs = New-Object IO.FileStream($dbFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
        $fs.Close()
        return $true
    } catch { return $false }
}

function Find-Database {
    if ($DbPath) { return $DbPath }
    $roots = @(
        (Join-Path $env:USERPROFILE 'Documents\xwechat_files'),
        (Join-Path $env:USERPROFILE 'Documents\WeChat Files')
    )
    $iniDir = Join-Path $env:APPDATA 'Tencent\xwechat\config'
    if (Test-Path $iniDir) {
        foreach ($ini in Get-ChildItem $iniDir -Filter *.ini -ErrorAction SilentlyContinue) {
            try { $lines = [IO.File]::ReadAllLines($ini.FullName, [Text.Encoding]::Default) } catch { continue }
            for ($i = 0; $i -lt $lines.Count; $i++) {
                if ($lines[$i] -match '^\s*MyDocument\s*:\s*(.*)$') {
                    $val = $matches[1].Trim()
                    if (-not $val -and ($i + 1) -lt $lines.Count) { $val = $lines[$i + 1].Trim() }
                    if ($val -and (Test-Path $val) -and ($roots -notcontains $val)) { $roots = @($val) + $roots }
                }
            }
        }
    }
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $cand = Get-ChildItem $root -Recurse -Filter 'message_0.db' -File -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($cand) { return $cand.FullName }
    }
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $cand = Get-ChildItem $root -Recurse -Filter '*.db' -File -ErrorAction SilentlyContinue |
                Sort-Object Length -Descending | Select-Object -First 1
        if ($cand) { return $cand.FullName }
    }
    return $null
}

# ---------- 自检 ----------
if ($SelfTest) {
    $keyHex = -join (1..32 | ForEach-Object { '{0:x2}' -f (Get-Random -Minimum 0 -Maximum 256) })
    $key = ConvertFrom-Hex $keyHex
    $plain = New-Object byte[] 4000
    $magic = [Text.Encoding]::ASCII.GetBytes('SQLite format 3')
    [Array]::Copy($magic, 0, $plain, 0, 15)
    $iv = New-Object byte[] 16
    (New-Object Random).NextBytes($iv)
    $aes = [System.Security.Cryptography.Aes]::Create()
    $aes.Mode = 'CBC'; $aes.Padding = 'None'; $aes.Key = $key; $aes.IV = $iv
    $enc = $aes.CreateEncryptor().TransformFinalBlock($plain, 0, $plain.Length)
    $page = New-Object byte[] 4096
    (New-Object Random).NextBytes($page)
    [Array]::Copy($enc, 0, $page, 16, $enc.Length)
    [Array]::Copy($iv, 0, $page, 4016, 16)
    $tmp = Join-Path $env:TEMP ('wxkey-selftest-' + [guid]::NewGuid().ToString('N') + '.db')
    [IO.File]::WriteAllBytes($tmp, $page)

    $ok = Test-Key $keyHex $tmp
    $bad = Test-Key ('00' * 32) $tmp
    Remove-Item $tmp -Force

    $buf = [Text.Encoding]::ASCII.GetBytes("xx x'" + ('ab' * 32) + ('cd' * 16) + "' yy")
    $hits = [WxKey]::FindInBuffer($buf, $buf.Length)

    Write-Host "解密自检: $ok (应为 True)"
    Write-Host "错误密钥: $bad (应为 False)"
    Write-Host "模式匹配: $($hits.Count) 个, 长度=$($hits[0].Length)"
    if ($ok -and (-not $bad) -and $hits.Count -eq 1 -and $hits[0].Length -eq 96) { Write-Host '自检通过'; exit 0 }
    Write-Host '自检失败'; exit 1
}

# ---------- 正式流程 ----------
$procs = @(Get-Process -Name 'Weixin', 'WeChat' -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending)
$hits = New-Object System.Collections.Generic.List[string]
$seen = New-Object System.Collections.Generic.HashSet[string]
foreach ($pr in $procs) {
    [string]$e = $null
    $h = [WxKey]::Scan($pr.Id, [ref]$e)
    if ($e) { continue }
    foreach ($x in $h) { if ($seen.Add($x)) { [void]$hits.Add($x) } }
}
foreach ($h in $hits) {
    if ($h.Length -ge 96) { Write-Output ("{0}:{1}" -f $h.Substring($h.Length - 32), $h.Substring(0, 64)) }
}