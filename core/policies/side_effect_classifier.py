# -*- coding: utf-8 -*-
"""SideEffectClassifier — Bash/Shell 命令侧效分类（V0.3-D，主人方案 §十一）。

不追求完美 Shell 分析，但绝不允许"无分类所以不管"。
五级：READ_ONLY / LOCAL_WORKING_WRITE / NETWORK_ACCESS /
      EXTERNAL_SIDE_EFFECT / UNKNOWN_SIDE_EFFECT
"""
import re

# 只读白名单（词首精确匹配）
_READ_ONLY_CMDS = {
    "cat", "ls", "head", "tail", "grep", "rg", "find", "pwd", "whoami",
    "date", "wc", "which", "file", "stat", "du", "df", "env", "printenv",
    "hostname", "uname", "id", "sleep", "true", "false", "test", "[",
    "diff", "sort", "uniq", "cut", "awk", "sed -n", "tee /dev/null",
    # 纯输出命令（重定向写由 has_redirect_write 单独判定）：
    "echo", "printf", "basename", "dirname", "seq", "type",
}
_READ_ONLY_GIT = {"git status", "git log", "git diff", "git show",
                  "git branch", "git remote", "git blame", "git rev-parse",
                  "git config --get"}

# 本地工作区写（白名单意图级）
_LOCAL_WRITE_CMDS = {"mkdir", "touch", "cp", "mv", "ln", "tar", "unzip",
                     "gunzip", "chmod", "sed -i", "patch"}
_LOCAL_WRITE_GIT = {"git add", "git commit", "git stash", "git checkout",
                    "git restore", "git rm", "git mv", "git tag"}

# 网络访问
_NETWORK_PAT = re.compile(
    r"\b(curl|wget|ping|nslookup|dig|ssh|scp|sftp|ftp|telnet|nc|netcat|"
    r"pip install|pip download|npm install|npm publish|yarn add|"
    r"git push|git pull|git fetch|git clone|svn|rsync)\b")

# 外部副作用（恒 fail-closed）
_EXTERNAL_PAT = re.compile(
    r"\b(git push|npm publish|scp\s+\S+\s+\S+:|rsync\b[^|]*::|"
    r"curl\s+(-X\s*)?(POST|PUT|DELETE|PATCH)|--upload-file|-d\s|-F\s|"
    r"shutdown|reboot)\b")

# 工作区外敏感路径（删除/写=外部级副作用）
# 注意：<workspace>/.workbuddy 是项目记忆目录不算敏感；用户级 ~/.workbuddy
# 由 Write/Edit 工具闸门兜底（V0.3-D 在线实证修正：heredoc 写项目记忆曾被误拦）
_SENSITIVE_PATH_PAT = re.compile(
    r"(^|\s|[/\\])(\.\.|C:\\Windows|/etc|/usr|/bin|/sbin|/System|"
    r"Desktop|Downloads|Documents|AppData|\.ssh)"
    r"(\s|$|[/\\])", re.IGNORECASE)

_DESTRUCTIVE_PAT = re.compile(
    r"\b(rm\s+-[rf]|del\s+/[sq]|rmdir\s+/s|Remove-Item\s+-Recurse|"
    r"mkfs|dd\s+if=|>\s*/dev/sd|format\s+)\b")

# 本地开发命令（测试/构建/解释器无网络特征时视为本地工作写）：
# V0.3-D 在线实证——OPEN_ENDED 任务中 python -m unittest 被 UNKNOWN_SIDE_EFFECT
# 拦截导致 Harness 无法自我回归；此类命令在无网络/破坏特征时归 LOCAL_WORKING_WRITE
_LOCAL_DEV_PAT = re.compile(
    r"^(python\s+-m\s+(unittest|pytest|pip|json|compileall)|python\s+-c\b|"
    r"py\s+-m\s+(unittest|pytest)|pytest\b|node\s+--test\b|"
    r"npm\s+(run|test)\b|git\s+diff\b)")


def _is_local_dev(seg):
    return bool(_LOCAL_DEV_PAT.match(seg.strip()))


def _split_top_level(cmd):
    """按 && || ; | 切分链段，跳过引号内内容。

    V0.3-F 在线实证修正：`grep -E "^(FAIL|ERROR):"` 引号内的 `|` 曾被当作
    管道符切分，产生出 "ERROR):" 这样的伪段 → 整条命令误判 UNKNOWN_SIDE_EFFECT
    → OPEN_ENDED 高风险误 deny（会污染 enforce 证据指标）。
    """
    segs, buf, quote = [], [], None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if cmd.startswith("&&", i) or cmd.startswith("||", i):
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|":
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return segs


def classify_bash(command):
    """返回侧效级：READ_ONLY / LOCAL_WORKING_WRITE / NETWORK_ACCESS /
    EXTERNAL_SIDE_EFFECT / UNKNOWN_SIDE_EFFECT。"""
    cmd = (command or "").strip()
    if not cmd:
        return "READ_ONLY"

    # 外部副作用 / 破坏性命令优先（含跨链段，&& ; | 全串检测）
    if _DESTRUCTIVE_PAT.search(cmd):
        return "EXTERNAL_SIDE_EFFECT"
    if _EXTERNAL_PAT.search(cmd):
        return "EXTERNAL_SIDE_EFFECT"
    if _SENSITIVE_PATH_PAT.search(cmd) and re.search(
            r"\b(rm|del|rmdir|mv|cp|Remove-Item|echo|>>?|tee)\b", cmd):
        return "EXTERNAL_SIDE_EFFECT"

    has_redirect_write = bool(re.search(r"(^|\s)(>>?|tee\s)\s*\S", cmd))

    # 链段拆分（&& || ; |，引号感知）逐段判只读/本地写/未知
    segments = _split_top_level(cmd)
    all_known = True       # 所有段均属只读或本地写白名单
    any_local_write = False
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split()
        head = parts[0] if parts else ""
        token = " ".join(parts[:2])
        if head in _READ_ONLY_CMDS or token in _READ_ONLY_GIT \
                or token in _READ_ONLY_CMDS:
            continue
        if head in _LOCAL_WRITE_CMDS or token in _LOCAL_WRITE_GIT:
            all_known = True and all_known
            any_local_write = True
            continue
        if re.match(r"^(cd|set|export)\b", seg):
            continue
        if _is_local_dev(seg):
            any_local_write = True
            continue
        all_known = False
        break

    if _NETWORK_PAT.search(cmd):
        return "NETWORK_ACCESS"
    if all_known and not any_local_write and not has_redirect_write:
        return "READ_ONLY"
    if all_known:
        # 只读+本地写组合（含纯重定向写），无网络特征 → 本地工作写
        return "LOCAL_WORKING_WRITE"
    # 出现未知段（脚本执行等）
    return "UNKNOWN_SIDE_EFFECT"


HIGH_RISK_LEVELS = {"NETWORK_ACCESS", "EXTERNAL_SIDE_EFFECT", "UNKNOWN_SIDE_EFFECT"}
FAIL_CLOSED_LEVELS = {"EXTERNAL_SIDE_EFFECT"}  # 恒 fail-closed（不受 shadow 豁免）
