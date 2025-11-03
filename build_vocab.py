from pathlib import Path
import argparse
import os
import re
import sys
import shutil

# ===== 入口查找 =====
def find_target(version_prefix: str | None, base: Path):
    pat = re.compile(r"^vocab_v(\d+(?:\.\d+)*)\.py$", re.IGNORECASE)
    cands = []
    for p in base.glob("vocab_v*.py"):
        m = pat.match(p.name)
        if not m:
            continue
        ver = tuple(int(x) for x in m.group(1).split("."))
        cands.append((ver, p))
    if not cands:
        sys.exit(f"未找到版本脚本：{base}\\vocab_v*.py")
    if version_prefix:
        want = tuple(int(x) for x in version_prefix.split(".") if x.isdigit())
        m = sorted((c for c in cands if c[0][:len(want)] == want), reverse=True)
        if not m:
            sys.exit(f"未找到指定版本前缀：{version_prefix}")
        return m[0]
    return max(cands)

# ===== add-data helpers =====
def _add_data_file_to_dir(args: list[str], src: Path, dest_dir: str):
    """
    单文件“文件→目录”映射：src -> dest_dir/src.name
    注意：PyInstaller 要求 --add-data 的第二段是“目录”，不能包含文件名！
    """
    sep = ";" if os.name == "nt" else ":"
    # 只传目录，不拼接文件名，避免生成同名目录套文件
    args += ["--add-data", f"{src.resolve()}{sep}{dest_dir.rstrip('/')}"]

def _add_data_tree(args: list[str], src_dir: Path, dest_dir_in_pkg: str):
    """目录整体映射（仅用于非字体目录）"""
    sep = ";" if os.name == "nt" else ":"
    args += ["--add-data", f"{src_dir.resolve()}{sep}{dest_dir_in_pkg.rstrip('/')}"]

# ===== 预清理，避免两个 exe 并存 =====
def clean_outputs(script_dir: Path, name: str):
    dist = script_dir / "dist"
    build = script_dir / "build"
    spec = script_dir / f"{name}.spec"
    targets = [dist / name, dist / f"{name}.exe", build, spec]
    for t in targets:
        try:
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
            elif t.exists():
                t.unlink(missing_ok=True)
        except Exception:
            pass

# ===== 构建 =====
def build_with_pyinstaller(entry: Path, name: str, onefile=True, console=False, script_dir: Path | None = None):
    try:
        import PyInstaller.__main__ as pimain
        import PyInstaller
        ver = getattr(PyInstaller, "__version__", "unknown")
    except Exception:
        sys.exit("未安装 PyInstaller，请先执行：pip install pyinstaller")

    if script_dir is None:
        script_dir = Path(__file__).resolve().parent

    # 清理旧产物
    clean_outputs(script_dir, name)

    args = [
        str(entry),
        "--name", name,
        "--clean",
        "--noconfirm",
        "--log-level=INFO",
        "--onefile" if onefile else "--onedir",
    ]
    if not console:
        args.append("--noconsole")

    # 可选：固定运行时解包目录，减少 Temp 被锁
    rtmp = os.path.join(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")), "vocab_rt")
    try:
        os.makedirs(rtmp, exist_ok=True)
        args += ["--runtime-tmpdir", rtmp]
        print("Runtime tmpdir:", rtmp)
    except Exception:
        pass

    # 非字体资源
    tools_dir = script_dir / "tools"
    if tools_dir.is_dir():
        _add_data_tree(args, tools_dir, "tools")

    icon_ico = script_dir / "assets" / "app.ico"
    if icon_ico.exists():
        args += ["--icon", str(icon_ico)]

    # ===== 字体：仅做“文件→目录”映射，按目标文件名去重 =====
    font_exts = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}
    added = []
    used_names = set()  # 去重：同名字体只打一次

    def try_add_file(p: Path):
        if p.is_file() and p.suffix.lower() in font_exts:
            key = p.name.lower()
            if key in used_names:
                return
            used_names.add(key)
            _add_data_file_to_dir(args, p, "assets/fonts")
            added.append(p)

    # 优先 assets/fonts 下的字体
    af = script_dir / "assets" / "fonts"
    if af.is_dir():
        for q in af.rglob("*"):
            try_add_file(q)
    # 其次 根目录
    for q in script_dir.glob("*"):
        try_add_file(q)
    # 再次 ./fonts
    fonts_dir = script_dir / "fonts"
    if fonts_dir.is_dir():
        for q in fonts_dir.rglob("*"):
            try_add_file(q)

    print(f"PyInstaller 版本：{ver}")
    if added:
        print("将打包以下字体到 assets/fonts/：")
        for f in added:
            print("  -", f)
    else:
        print("未发现可打包字体；请把字体放在脚本同目录或 assets/fonts/ 或 fonts/。")

    # ===== 强校验与自动修正 =====
    # 1) 允许“文件→目录”（正确用法）
    # 2) 禁止“目录→.ttf/.otf/... 文件路径”
    # 3) 如发现“文件→assets/fonts/xxx.ttf”这种错误写法，则自动改为“文件→assets/fonts”
    sep = ";" if os.name == "nt" else ":"
    i = 0
    while i < len(args):
        if args[i] == "--add-data" and i + 1 < len(args):
            src_s, dst_s = args[i + 1].split(sep, 1)
            src = Path(src_s)
            dst = dst_s.replace("\\", "/").rstrip("/")
            # 目录→文件路径：报错退出
            if src.is_dir() and dst.lower().endswith((".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2")):
                print("ERROR: 发现错误映射（目录→.ttf/.otf 文件路径）：", args[i + 1])
                sys.exit(1)
            # 文件→包含文件名的路径：自动修正为父目录，避免同名目录
            if src.is_file() and dst.lower().endswith((".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2")):
                fixed = "/".join(dst.split("/")[:-1]) or "."
                print(f"修正映射：{args[i + 1]}  =>  {src_s}{sep}{fixed}")
                args[i + 1] = f"{src_s}{sep}{fixed}"
        i += 1

    print("PyInstaller 命令参数：", args)
    pimain.run(args)

    # 输出本次生成的 exe 路径
    exe_path = script_dir / "dist" / (f"{name}.exe" if onefile else f"{name}\\{name}.exe")
    print("本次生成的 EXE：", exe_path)
    if not onefile:
        # onedir 自检（PyInstaller v6+：_internal 为资源目录）
        target_new = script_dir / "dist" / name / "_internal" / "assets" / "fonts"
        target_old = script_dir / "dist" / name / "assets" / "fonts"
        font_dir = target_new if target_new.exists() else target_old
        print("onedir 字体目录：", font_dir)

def main():
    script_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="打包 vocab 最新或指定版本（字体采用 文件→目录 映射）")
    ap.add_argument("--version", "-v", default=None, help="版本前缀（如 3.3 或 3.3.4）")
    ap.add_argument("--name", "-n", default=None, help="可执行文件名（缺省：按版本或 vocab_latest）")
    ap.add_argument("--onedir", action="store_true", help="使用 onedir（默认 onefile）")
    ap.add_argument("--console", action="store_true", help="保留控制台（默认隐藏）")
    args = ap.parse_args()

    ver = args.version or os.environ.get("VOCAB_VERSION") or None
    ver_tuple, entry = find_target(ver, script_dir)
    ver_str = ".".join(str(x) for x in ver_tuple)
    name = args.name or (f"vocab_v{ver_str}" if ver else "vocab_latest")

    print(f"脚本目录：{script_dir}")
    print(f"将打包：{entry.name}（版本 {ver_str}） -> 可执行名：{name}")
    build_with_pyinstaller(
        entry, name,
        onefile=(not args.onedir),
        console=bool(args.console),
        script_dir=script_dir
    )

if __name__ == "__main__":
    main()
