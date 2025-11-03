from pathlib import Path
import os, re, sys, subprocess

def find_latest_script():
    pattern = re.compile(r"^vocab_v(\d+(?:\.\d+)*)\.py$", re.IGNORECASE)
    candidates = []
    for p in Path(".").glob("vocab_v*.py"):
        m = pattern.match(p.name)
        if not m:
            continue
        ver_tuple = tuple(int(x) for x in m.group(1).split("."))
        candidates.append((ver_tuple, p))
    if not candidates:
        sys.exit("未找到版本脚本：vocab_v*.py")

    want = os.getenv("VOCAB_VERSION", "").strip()
    if want:
        want_parts = tuple(int(x) for x in want.split(".") if x.isdigit())
        for ver, path in sorted(candidates, reverse=True):
            if ver[:len(want_parts)] == want_parts:
                return path
        sys.exit(f"未找到指定版本：{want}")

    return max(candidates)[1]

def main():
    target = find_latest_script()
    # 透传命令行参数
    cmd = [sys.executable, str(target), *sys.argv[1:]]
    # 使用子进程运行，避免 PyQt5 主进程上下文干扰
    raisecode = subprocess.call(cmd)
    sys.exit(raisecode)

if __name__ == "__main__":
    main()