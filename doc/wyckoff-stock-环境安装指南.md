# OpenClaw / ClawHub CLI 安装 + wyckoff-stock 技能安装指南

> 目标：在 Windows 上装好环境，并安装 dxarch1980 的「Wyckoff Stock（A股威科夫诊股）」技能
> 整理日期：2026-11-09
> 仅作技术参考，不构成投资建议。

---

## 一、前置依赖：Node.js

| 依赖 | 最低版本 | 说明 |
|------|----------|------|
| **Node.js** | 22.13+（推荐 24） | 不支持 Node 23 |
| **npm** | 10.0.0+ | 随 Node.js 附带 |
| **Git** | 2.0+ | 可选（部分功能需要） |

### 检查当前版本

```bash
node -v
npm -v
```

### Windows 安装 Node.js（推荐 winget）

```powershell
winget install OpenJS.NodeJS.LTS
```

> 也可从官网 https://nodejs.org 下载 LTS 安装包。

### 国内加速（可选，强烈建议）

```bash
npm config set registry https://registry.npmmirror.com
```

---

## 二、安装 ClawHub CLI（二选一）

### 方式 A：npx 直接调用（无需全局安装，最省事）

```bash
npx clawhub@latest install wyckoff-stock
```

### 方式 B：全局安装后使用

```bash
npm i -g clawhub
clawhub install wyckoff-stock
```

> `clawhub install <slug>` 会自动解析最新版本、下载 zip 包，解压到**当前工作目录下的 `./skills`** 文件夹，并写入锁文件 `.clawhub/lock.json`。
> 可用 `--workdir <目录>` 指定安装位置，`--dir <目录>` 指定安装子目录。
> 需要 Node.js 22.13+ 或 24+。

---

## 三、安装 wyckoff-stock 技能

### 通过 ClawHub CLI（推荐）

```bash
npx clawhub@latest install wyckoff-stock
# 或
clawhub install wyckoff-stock
```

### 通过 OpenClaw CLI（替代方式）

先装 OpenClaw CLI：

```bash
npm install -g openclaw
# 或 pnpm
pnpm add -g openclaw@latest && openclaw onboard --install-daemon
```

再装技能：

```bash
openclaw skills install @dxarch1980/wyckoff-stock
```

### 同系列其他技能（可选）

```bash
npx secure-skills-cli add dxarch1980/wyckoff-screen      # 选股
npx secure-skills-cli add dxarch1980/wyckoff-diagnose    # 诊股
```

---

## 四、安装 Python 依赖（运行技能本体需要）

```bash
pip install akshare --upgrade -i https://mirrors.aliyun.com/pypi/simple/
pip install efinance
```

---

## 五、常用管理命令（OpenClaw / ClawHub）

```bash
openclaw skills list              # 列出已安装技能
openclaw skills list --eligible   # 查看可用技能
openclaw skill info <name>        # 查看技能详情
openclaw skill update <name>      # 更新技能
openclaw skill uninstall <name>   # 卸载技能
openclaw skill enable <name>      # 启用
openclaw skill disable <name>     # 禁用
openclaw skill reload             # 重新加载

clawhub search "关键词"           # ClawHub 搜索
```

---

## 六、常见问题排查

### 1. `openclaw: command not found`（全局 bin 不在 PATH）

```bash
npm prefix -g
export PATH="$(npm prefix -g)/bin:$PATH"   # 添加到 ~/.bashrc 或 ~/.zshrc
```

### 2. Windows 执行脚本被阻止（PowerShell 执行策略）

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 3. `npm install -g` 权限错误（EACCES，Linux/macOS）

```bash
mkdir -p "$HOME/.npm-global"
npm config set prefix "$HOME/.npm-global"
export PATH="$HOME/.npm-global/bin:$PATH"
```

### 4. 国内下载慢

```bash
npm config set registry https://registry.npmmirror.com
# Node 版本管理器镜像（nvm）：
# NVM_NODEJS_ORG_MIRROR=https://npmmirror.com/mirrors/node
```

---

## 七、安装完成后的完整流程回顾

1. 装 **Node.js 22.13+/24**（winget 或官网）
2. 装 **ClawHub CLI**（`npm i -g clawhub` 或直接 npx）
3. `clawhub install wyckoff-stock`（或 npx 方式）
4. `pip install akshare efinance`
5. 进入技能目录，运行诊股脚本（参考桌面另一份文档 `wyckoff-stock-安装与运行.md`）

---

## 八、相关链接

- Wyckoff Stock 文档：https://docs.clawhub.ai/dxarch1980/skills/wyckoff-stock
- OpenClaw CLI 文档：https://docs.openclaw.ai/clawhub/cli
- ClawHub（npm）：https://www.npmjs.com/package/clawhub
