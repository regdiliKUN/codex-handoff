# 发行包 · Codex 交接助手

本目录存放 **Codex 交接助手** 的 Windows x64 可运行包体（由桌面快捷方式实际指向的安装/发布目录整理而来）。

| 项目 | 内容 |
| --- | --- |
| 版本 | `1.2.0` |
| 平台 | Windows x64 |
| 包体路径 | `release/1.2.0/codex-handoff-1.2.0-win-x64/` |
| 文件数 | 13 |
| 体积 | 约 14.0 MB |

## 运行方式

双击 `CodexHandoff.exe`

## 校验完整性

```bash
cd release
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell：

```powershell
Get-Content release\SHA256SUMS.txt | ForEach-Object {
  $h, $p = $_ -split '  ', 2
  if ((Get-FileHash $p -Algorithm SHA256).Hash -ne $h.ToUpper()) { "MISMATCH $p" }
}
```

## 关于 Codex 交接助手

Windows 图形化 Codex 本地聊天交接工具：自动备份、文件校验、可选项目携带与文件级增量快照。

## 未纳入的内容

PyInstaller 中间产物 `build/` 未纳入。

以上目录属于开发环境、运行期缓存或大体积模型权重，不影响程序本体运行。
