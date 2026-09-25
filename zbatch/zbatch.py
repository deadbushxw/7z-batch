# -*- coding: utf-8 -*-
"""
7z 批量压缩 / 解压工具

本质是 7z.exe 的可视化批处理外壳，零第三方依赖（只用 Python 标准库 + tkinter）。

命名规则
  {YYYYMMDD}        取自文件修改时间(mtime)，跨天的文件分到不同日期
  文件包            {YYYYMMDD}{NN}.7z     一个文件一个包，同一天内按时间从老到新编号 01、02…
  目录包            {YYYYMMDD}index.7z    当天清单，不占序号，每天一份，重跑时原地更新
  清单本体          {YYYYMMDD}index.txt   在目录包内，UTF-8 with BOM
  压缩后源文件      _{原文件名}.{原扩展名}
  解压出的文件      _{原文件名}.{原扩展名}

同一天多次运行
  文件包序号从「磁盘上当天已有包的最大序号」往后顺延，绝不覆盖、不重排
  目录包在每次运行结束时按当天全量重建（先写临时文件再原子改名）

压缩参数（对齐参考图）
  -t7z        7z 格式
  -mx=0       压缩等级 0 - 仅存储
  -mhe=on     加密文件名
  -p<pwd>     AES-256 加密（密码为空时 7z 根本不加密，所以界面强制要求输入）
  cwd=工作目录 只传文件名 → 包内为相对路径
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_TITLE = "7z 批量压缩 / 解压工具"
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_NAME = Path(__file__).name
LAUNCHER_NAME = "zbatch.cmd"
SELFTEST_NAME = "zbatch_selftest.py"
README_NAME = "README.md"
GITIGNORE_NAME = ".gitignore"
GITATTRIBUTES_NAME = ".gitattributes"
LICENSE_NAME = "LICENSE"
CONFIG_NAME = "zbatch.config.json"

# 属于工具/项目自身的文件，一律不参与压缩、不被一键删除。
# 点开头的文件另有通用规则（见 is_tool_artifact），这里列的是不带点的项目文件。
SELF_NAMES = {
    SCRIPT_NAME,
    LAUNCHER_NAME,
    SELFTEST_NAME,
    README_NAME,
    GITIGNORE_NAME,
    GITATTRIBUTES_NAME,
    LICENSE_NAME,
    CONFIG_NAME,
}

MARK = "_"  # 标记前缀：压缩后源文件、解压出的文件都用它

RE_FILE_ARCHIVE = re.compile(r"^(\d{8})(\d{2,3})\.7z$", re.IGNORECASE)
RE_INDEX_ARCHIVE = re.compile(r"^(\d{8})index\.7z$", re.IGNORECASE)
RE_BARE_INDEX_TXT = re.compile(r"^\d{8}index\.txt$", re.IGNORECASE)

# 清单里给工具自己读的那一行（人看的表格保持不变）
MANIFEST_JSON_TAG = "#ZBATCH1 "
# 旧清单没有上面那行时的兜底解析：序号 包名 原文件名 大小 打包时间
RE_MANIFEST_ROW = re.compile(
    r"^\s*\S+\s+(?P<arc>\S+\.7z)\s+(?P<name>.+?)\s+"
    r"(?P<size>\d+(?:\.\d+)?\s*[KMGTP]?B)\s+"
    r"(?P<packed>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$"
)

SEVEN_EXE_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]

EXIT_MEANING = {
    0: "成功",
    1: "警告（部分文件未处理）",
    2: "致命错误（密码错误或包已损坏）",
    7: "命令行错误",
    8: "内存不足",
    255: "被中断",
}
RET_CANCELLED = -1


def find_7z() -> str | None:
    """先查 PATH，再查常见安装位置。"""
    for name in ("7z", "7za", "7zr"):
        hit = shutil.which(name)
        if hit:
            return hit
    for cand in SEVEN_EXE_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def decode_output(raw: bytes) -> str:
    """7z 加了 -sccUTF-8，正常是 UTF-8；解码失败再退回本地代码页。"""
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def human_size(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def disp_width(s: str) -> int:
    """终端/记事本里的显示宽度：中日韩全角字符占两列。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s: str, width: int, align: str = "left") -> str:
    gap = max(width - disp_width(s), 0)
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def truncate(s: str, width: int) -> str:
    """按显示宽度截断，超出加省略号。"""
    if disp_width(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


def date_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y%m%d")


def date_key(label: str) -> str:
    """
    下拉框里给人看的是 2026-09-25，内部键和包名前缀是 20260925。
    凡是拿下拉框的值去查表或拼包名，都要先过这里。
    """
    return re.sub(r"\D", "", label or "")


def archive_date(name: str) -> str | None:
    """从包名里取日期前缀，认不出返回 None（比如随手放进来的 backup.7z）。"""
    m = RE_FILE_ARCHIVE.match(name) or RE_INDEX_ARCHIVE.match(name)
    return m.group(1) if m else None


def inner_names(z: SevenZip, archive: Path) -> str:
    """
    直接读一个包里的原始文件名（清单覆盖不到时的兜底路径）。
    读不出时如实说明原因——这比留空有用得多，能直接看出是密码不对还是包坏了。
    """
    code, entries = z.list_entries(archive)
    if code != 0:
        return "⚠ 读不出（密码不对或包已损坏）"
    if not entries:
        return "⚠ 包内没有文件"
    label, _size = summarize_entries(entries)
    return truncate(label, 60)


def read_day_manifest(z: SevenZip, workdir: Path, date: str) -> dict[str, tuple[list[str], str]]:
    """
    解开某天的 {YYYYMMDD}index.7z 并解析出 {包名: (包内文件名列表, 打包时间)}。
    天气不对、包不存在、内容读不出，一律返回空字典（调用方会退回逐个读包）。
    """
    arc = workdir / f"{date}index.7z"
    if not arc.is_file():
        return {}
    try:
        with tempfile.TemporaryDirectory(prefix="zbatch_idx_") as td:
            code, _out = z.extract_to(arc, Path(td))
            if code != 0:
                return {}
            txts = sorted(Path(td).rglob("*.txt"))
            if not txts:
                return {}
            return parse_manifest(txts[0].read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def is_tool_artifact(name: str) -> bool:
    """
    工具自身、项目自身的文件，任何时候都不作为素材——即使勾了「包含点开头的文件」。
    """
    if name in SELF_NAMES:
        return True
    if name.lower().endswith(".7z.tmp"):
        return True
    if RE_BARE_INDEX_TXT.match(name):
        return True
    return False


def is_dotfile(name: str) -> bool:
    return name.startswith(".") and name not in (".", "..")


# ---------------------------------------------------------------------------
# 界面尺寸
#
# 进程声明了 DPI 感知后，Tk 的坐标是物理像素，而字号会按 DPI 放大，
# 于是写死的 1100x740 在 200% 缩放的屏幕上会把底部状态条挤出窗口。
# 所有像素量都乘以这里的缩放系数。
# ---------------------------------------------------------------------------


def ui_scale(root: tk.Tk) -> float:
    try:
        return min(max(root.winfo_fpixels("1i") / 96.0, 1.0), 3.0)
    except Exception:
        return 1.0


def px(value: float, scale: float) -> int:
    return int(round(value * scale))


# ---------------------------------------------------------------------------
# 7z 封装
# ---------------------------------------------------------------------------


class SevenZip:
    """只管调用 7z.exe，不碰界面。"""

    def __init__(self, exe: str, password: str = "", cancel: threading.Event | None = None):
        self.exe = exe
        self.password = password
        self.cancel = cancel or threading.Event()
        self.proc: subprocess.Popen | None = None

    # -- 底层调用 ---------------------------------------------------------

    def run(self, switches: list[str], operands: list[str] | tuple = (), cwd: Path | None = None,
            timeout: float = 6 * 3600):
        """
        返回 (exit_code, 合并后的输出)。取消时返回 RET_CANCELLED。

        参数分两段：switches 是开关，operands 是文件/包名。
        全局开关与 `--` 由这里统一拼装，保证 `--` 之后只剩操作数——
        否则 -sccUTF-8 之类会被 7z 当成文件名。
        """
        cmd = [self.exe, *switches, "-sccUTF-8", "-bsp0", "--", *operands]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        elapsed = 0.0
        while True:
            try:
                out, _ = self.proc.communicate(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                elapsed += 0.4
                if self.cancel.is_set():
                    self.proc.kill()
                    out, _ = self.proc.communicate()
                    return RET_CANCELLED, decode_output(out or b"") + "\n[已取消]"
                if elapsed > timeout:
                    self.proc.kill()
                    out, _ = self.proc.communicate()
                    return 8, decode_output(out or b"") + "\n[超时终止]"
        return self.proc.returncode, decode_output(out or b"")

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _pw(self) -> list[str]:
        return [f"-p{self.password}"]

    # -- 压缩 -------------------------------------------------------------

    def add_one(self, workdir: Path, archive_name: str, filename: str):
        """把 workdir 下的单个文件打成 archive_name（同目录、包内相对路径）。"""
        switches = [
            "a",
            "-t7z",
            "-mx=0",          # 图一：压缩等级 0 - 仅存储
            "-mhe=on",        # 图一：加密文件名
            *self._pw(),
            "-y",
            "-spd",           # 关闭通配符展开，文件名原样处理
        ]
        return self.run(switches, [archive_name, filename], cwd=workdir)

    def list_entries(self, archive: Path):
        """
        按 -slt 列出包内条目，返回 (code, [dict])。
        剔除包自身的表头条目（带 Headers Size）和目录条目（Attributes 以 D 开头），
        只留真正的文件。
        """
        code, out = self.run(["l", "-slt", *self._pw(), "-spd"], [str(archive)])
        entries: list[dict[str, str]] = []
        cur: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                if cur:
                    entries.append(cur)
                    cur = {}
                continue
            if " = " in line:
                k, v = line.split(" = ", 1)
                cur[k.strip()] = v.strip()
        if cur:
            entries.append(cur)
        files = []
        for e in entries:
            if "Headers Size" in e or "Path" not in e:
                continue
            if e.get("Attributes", "").startswith("D"):
                continue
            files.append(e)
        return code, files

    def verify_archive(self, archive: Path, expect_name: str, expect_size: int):
        """
        打包后核对。7z 遇到含 * ? 之类的病态文件名会静默产出空包并报成功，
        所以这一步是必需的，不是可选优化。返回 (ok, 说明文字)。
        """
        if not archive.is_file():
            return False, "压缩包没有生成"
        code, entries = self.list_entries(archive)
        if code != 0:
            return False, f"无法读取刚生成的包（7z 退出码 {code}）"
        if not entries:
            return False, "包内没有任何文件（7z 静默失败）"
        if len(entries) > 1:
            return False, f"包内有 {len(entries)} 个条目，预期 1 个"
        ent = entries[0]
        got = ent.get("Path", "").replace("\\", "/").split("/")[-1]
        if got != expect_name:
            return False, f"包内文件名不符：期望 {expect_name}，实际 {got}"
        try:
            size = int(ent.get("Size", "-1"))
        except ValueError:
            size = -1
        if size != expect_size:
            return False, f"包内文件大小不符：期望 {expect_size}，实际 {size}"
        # 0 字节文件没有数据流，7z 无可加密，条目会显示 Encrypted = -，
        # 但表头依然是加密的（这正是我们要求的），所以此时不该判为失败。
        if expect_size > 0 and ent.get("Encrypted") != "+":
            return False, "包内文件未加密"
        suffix = "，空文件仅加密文件名" if expect_size == 0 else ""
        return True, f"已核对 {human_size(expect_size)}{suffix}"

    def test_archive(self, archive: Path):
        """完整读一遍包做 CRC 校验，大文件耗时翻倍，所以做成可选项。"""
        code, out = self.run(["t", *self._pw()], [str(archive)])
        if code == 0:
            return True, "完整性校验通过"
        return False, f"完整性校验失败（退出码 {code}）\n{out.strip()[-400:]}"

    # -- 解压 -------------------------------------------------------------

    def extract_to(self, archive: Path, dest: Path):
        dest.mkdir(parents=True, exist_ok=True)
        switches = ["x", *self._pw(), "-y", "-spd", f"-o{dest}"]
        return self.run(switches, [str(archive)])


# ---------------------------------------------------------------------------
# 计划（纯逻辑）
# ---------------------------------------------------------------------------


@dataclass
class PlanItem:
    src: Path
    date: str
    seq: int
    archive_name: str
    size: int
    mtime: float


@dataclass
class ManifestRow:
    seq_label: str
    archive: str
    names: list[str]  # 包内的原始文件名（通常只有一个）
    size: int
    modified: str
    packed_at: str

    @property
    def original(self) -> str:
        return summarize_names(self.names)


@dataclass
class JobCtx:
    """任务与界面之间的唯一接口，方便脱离 tkinter 直接跑。"""

    log: Callable[[str, str], None]
    prog: Callable[[int, int, str], None]
    cancel: threading.Event
    full_verify: bool = False


def scan_candidates(
    workdir: Path, include_marked: bool, include_dotted: bool = False
) -> list[Path]:
    """
    扫描待压缩文件：只要工作目录第一层，不递归。

    点开头的文件默认跳过：这个工具常被放在项目（或归档）文件夹根上，
    .gitignore / .gitattributes / .editorconfig 这类东西属于仓库配置而不是待归档的资料。
    与其一个个往 SELF_NAMES 里加、加漏一个就被误打包，不如按前缀一刀切，
    需要时用界面上的勾选框放开。工具自身的文件则永远不碰。
    """
    files: list[Path] = []
    try:
        children = sorted(workdir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return files
    for p in children:
        if not p.is_file():
            continue
        if p.suffix.lower() == ".7z":
            continue
        if is_tool_artifact(p.name):
            continue
        if is_dotfile(p.name) and not include_dotted:
            continue
        if p.name.startswith(MARK) and not include_marked:
            continue
        files.append(p)
    return files


def scan_existing_archives(workdir: Path):
    """返回 (date -> {seq}, date -> {seq: 包名}, 所有日期集合)。"""
    seq_by_date: dict[str, set[int]] = {}
    name_by_date: dict[str, dict[int, str]] = {}
    dates: set[str] = set()
    try:
        children = list(workdir.iterdir())
    except OSError:
        return seq_by_date, name_by_date, dates
    for p in children:
        if not p.is_file():
            continue
        m = RE_FILE_ARCHIVE.match(p.name)
        if m:
            d, s = m.group(1), int(m.group(2))
            seq_by_date.setdefault(d, set()).add(s)
            name_by_date.setdefault(d, {})[s] = p.name
            dates.add(d)
            continue
        m2 = RE_INDEX_ARCHIVE.match(p.name)
        if m2:
            dates.add(m2.group(1))
    return seq_by_date, name_by_date, dates


def build_plan(files: list[Path], seq_by_date: dict[str, set[int]]) -> list[PlanItem]:
    """
    分天 + 编号。序号基线取当天磁盘已有包的最大序号 +1，
    这样同一天多次运行会自然衔接，不会覆盖也不会重排已有包。
    """
    by_date: dict[str, list[Path]] = {}
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        by_date.setdefault(date_of(st.st_mtime), []).append(f)

    items: list[PlanItem] = []
    for d in sorted(by_date):
        used = set(seq_by_date.get(d, set()))
        seq = (max(used) + 1) if used else 1
        for f in sorted(by_date[d], key=lambda x: (x.stat().st_mtime, x.name.lower())):
            while seq in used:
                seq += 1
            used.add(seq)
            st = f.stat()
            items.append(
                PlanItem(
                    src=f,
                    date=d,
                    seq=seq,
                    archive_name=f"{d}{seq:02d}.7z",
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
            seq += 1
    return items


def marked_name(name: str) -> str:
    """_{原文件名}.{原扩展名}。已带前缀的不再二次改名，避免出现 __名字。"""
    if name.startswith(MARK):
        return name
    return MARK + name


def unique_path(target: Path) -> Path:
    """目标已存在时追加 -2、-3，绝不覆盖。"""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 2
    while True:
        cand = target.with_name(f"{stem}-{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def render_manifest(date: str, rows: list[ManifestRow], index_archive: str) -> str:
    """人可读的当天清单。按显示宽度对齐，中英混排也能对整齐。"""
    d_disp = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    w_seq = 6
    w_arc = max([disp_width(r.archive) for r in rows] + [10]) + 2
    w_name = max([disp_width(r.original) for r in rows] + [8]) + 2
    w_size = 10
    total = sum(r.size for r in rows)

    header = (
        pad("序号", w_seq) + pad("压缩包", w_arc) + pad("原文件名", w_name)
        + pad("大小", w_size, "right") + "  " + "打包时间"
    )
    line = "─" * disp_width(header)

    out = [
        f"{d_disp} 归档清单",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"目录包:   {index_archive}",
        "",
        line,
        header,
        line,
    ]
    for r in rows:
        out.append(
            pad(r.seq_label, w_seq) + pad(r.archive, w_arc) + pad(r.original, w_name)
            + pad(human_size(r.size), w_size, "right") + "  " + r.packed_at
        )
    out += [
        line,
        f"合计: {len(rows)} 个压缩包, {human_size(total)}",
        "",
        "说明：本文件是归档目录，记录每个压缩包内的原始文件名。",
        "     序号按当天文件修改时间从老到新排列，最老的为 01。",
        "     同一天多次批量压缩时，序号跨运行顺延，本清单为该天的全量累计。",
        "     一个包内若有多个文件，会并列在同一行的「原文件名」里。",
        f"     本清单本身打包为 {index_archive}，与文件包同一密码加密。",
        "",
        "# 下一行是工具自用的数据，请勿修改（上面的表格才是给人看的）",
        MANIFEST_JSON_TAG
        + json.dumps(
            {
                "v": 1,
                "date": date,
                "index": index_archive,
                "rows": [
                    {
                        "seq": r.seq_label,
                        "arc": r.archive,
                        "names": r.names,
                        "size": r.size,
                        "packed": r.packed_at,
                    }
                    for r in rows
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    ]
    return "\n".join(out) + "\n"


def parse_manifest(text: str) -> dict[str, tuple[list[str], str]]:
    """
    从清单里取出 {包名: (包内文件名列表, 打包时间)}。
    优先读机器可读那一行；没有（旧版本生成的清单）就按表格行解析兜底。
    """
    for ln in text.splitlines():
        if ln.startswith(MANIFEST_JSON_TAG):
            try:
                data = json.loads(ln[len(MANIFEST_JSON_TAG):])
            except ValueError:
                break
            out = {}
            for row in data.get("rows", []):
                arc = row.get("arc")
                if arc:
                    out[arc] = (list(row.get("names") or []), row.get("packed", ""))
            return out

    out = {}
    for ln in text.splitlines():
        m = RE_MANIFEST_ROW.match(ln)
        if m:
            out[m.group("arc")] = ([m.group("name").strip()], m.group("packed"))
    return out


# ---------------------------------------------------------------------------
# 工作目录里 MARK 前缀目标的收集（供一键删除用）
# ---------------------------------------------------------------------------


def collect_marked(workdir: Path) -> list[Path]:
    out: list[Path] = []
    try:
        children = sorted(workdir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for p in children:
        if not p.name.startswith(MARK):
            continue
        if p.name in SELF_NAMES:
            continue
        if p.is_file() and p.suffix.lower() == ".7z":
            continue  # 硬排除所有压缩包
        out.append(p)
    return out


def path_size(p: Path) -> int:
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def remove_path(p: Path):
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


# ---------------------------------------------------------------------------
# 任务（可脱离界面调用）
# ---------------------------------------------------------------------------


def summarize_names(names: list[str]) -> str:
    """一个包内多个文件名时怎么在一行里显示。"""
    if len(names) <= 3:
        return "、".join(names)
    return "、".join(names[:3]) + f" 等 {len(names)} 个文件"


def entry_names(entries: list[dict]) -> list[str]:
    """包内条目的相对路径（顶层文件就等于文件名，嵌套文件才看得出层级）。"""
    return [e.get("Path", "").replace("\\", "/").lstrip("/") for e in entries]


def entry_total(entries: list[dict]) -> int:
    total = 0
    for e in entries:
        try:
            total += int(e.get("Size", "0"))
        except ValueError:
            pass
    return total


def summarize_entries(entries: list[dict]) -> tuple[str, int]:
    """
    把一个包内的条目归纳成清单里的一行。
    本工具自己打的包是一个包一个文件，但外面的包可能装了多个、或者带层级，
    这种情况要如实记下来，而不是丢掉不记。
    """
    return summarize_names(entry_names(entries)), entry_total(entries)


def write_manifest(z: SevenZip, workdir: Path, date: str, ctx: JobCtx):
    """
    生成某一天的目录包。用 7z l 实际列出当天所有存量文件包，保证清单与磁盘一致，
    不去解析上一次的清单文件（避免格式耦合）。包名 {YYYYMMDD}index.7z，不占序号。
    """
    _seqs, name_by_date, _dates = scan_existing_archives(workdir)
    names = name_by_date.get(date, {})
    rows: list[ManifestRow] = []

    for seq in sorted(names):
        arc = workdir / names[seq]
        code, entries = z.list_entries(arc)
        if code != 0:
            ctx.log(f"清单汇总时跳过（读不出内容，密码不对或包已损坏）: {arc.name}", "warn")
            continue
        if not entries:
            ctx.log(f"清单汇总时跳过（包内没有文件）: {arc.name}", "warn")
            continue
        rows.append(
            ManifestRow(
                seq_label=f"{seq:02d}",
                archive=arc.name,
                names=entry_names(entries),
                size=entry_total(entries),
                modified=entries[0].get("Modified", ""),
                packed_at=fmt_time(arc.stat().st_mtime),
            )
        )

    if not rows:
        ctx.log(f"{date} 当天没有可汇总的文件包，未生成目录包", "warn")
        return None

    index_archive = f"{date}index.7z"
    index_txt = f"{date}index.txt"
    txt_path = workdir / index_txt
    tmp_arc = workdir / f"{index_archive}.tmp"

    try:
        txt_path.write_text(render_manifest(date, rows, index_archive), encoding="utf-8-sig")
    except OSError as exc:
        ctx.log(f"写清单失败: {exc}", "error")
        return None

    try:
        code, out = z.add_one(workdir, tmp_arc.name, index_txt)
        if code == RET_CANCELLED:
            ctx.log("已取消，未更新目录包", "warn")
            return None
        if code != 0:
            ctx.log(
                f"目录包压缩失败 [{EXIT_MEANING.get(code, code)}]: {out.strip()[-300:]}", "error"
            )
            return None
        tsize = txt_path.stat().st_size
        good, why = z.verify_archive(tmp_arc, index_txt, tsize)
        if not good:
            ctx.log(f"目录包核对不通过，已放弃: {why}", "error")
            return None
        # 原子替换：中途崩溃也不会毁掉当天唯一那份清单
        os.replace(tmp_arc, workdir / index_archive)
        ctx.log(f"目录包已更新: {index_archive}（汇总 {len(rows)} 条，{why}）", "ok")
        return index_archive
    finally:
        for junk in (txt_path, tmp_arc):
            try:
                if junk.exists():
                    junk.unlink()
            except OSError:
                pass


def build_manifests_job(z: SevenZip, workdir: Path, ctx: JobCtx) -> dict:
    """
    为工作目录里已有的文件包重建/更新当天目录包，不需要任何待压缩的源文件。
    用于「文件夹里只有格式化好的压缩包」的情况——压缩完清理掉源文件后再补目录，
    或者从别处拷来一批包想配上清单。
    """
    _seqs, names, _dates = scan_existing_archives(workdir)
    dates = [d for d in sorted(names) if names[d]]
    if not dates:
        ctx.log("没有找到 {YYYYMMDD}{NN}.7z 形式的文件包，无从生成目录", "warn")
        ctx.prog(0, 1, "没有可生成的文件包")
        return {"ok": 0, "fail": 0, "total": 0}

    ctx.log(f"将为 {len(dates)} 天生成/更新目录包", "info")
    ok = fail = 0
    for i, d in enumerate(dates, 1):
        if ctx.cancel.is_set():
            ctx.log("已取消，未处理的日期保持原样", "warn")
            break
        ctx.prog(i - 1, len(dates), f"正在生成目录包 {i}/{len(dates)}: {d}index.7z")
        if write_manifest(z, workdir, d, ctx):
            ok += 1
        else:
            fail += 1

    ctx.prog(len(dates), len(dates), f"生成目录包结束：成功 {ok}，失败 {fail}")
    ctx.log(f"生成目录包结束：成功 {ok}，失败 {fail}", "ok" if fail == 0 else "warn")
    return {"ok": ok, "fail": fail, "total": len(dates)}


def compress_job(z: SevenZip, workdir: Path, picked: list[PlanItem], ctx: JobCtx) -> dict:
    """逐文件打包 → 核对 → 源文件改名 → 生成当天目录包。"""
    total = len(picked)
    ok = fail = 0
    touched: set[str] = set()
    used_names: set[str] = set()
    ctx.log(f"开始压缩 {total} 个文件，按修改时间分天编号", "info")

    for n, item in enumerate(picked, 1):
        if ctx.cancel.is_set():
            ctx.log("已取消，未处理的文件保持原样", "warn")
            break
        ctx.prog(n - 1, total, f"正在压缩 {n}/{total}: {item.src.name}")

        src = workdir / item.src.name
        if not src.is_file():
            ctx.log(f"跳过（文件已不存在）: {item.src.name}", "warn")
            fail += 1
            continue

        # 目标名兜底：万一被人为放进同名包，顺延而不是覆盖
        arc_name, seq = item.archive_name, item.seq
        while (workdir / arc_name).exists() or arc_name in used_names:
            seq += 1
            arc_name = f"{item.date}{seq:02d}.7z"
        used_names.add(arc_name)

        size = src.stat().st_size
        code, out = z.add_one(workdir, arc_name, src.name)
        if code == RET_CANCELLED:
            ctx.log(f"已取消: {src.name}", "warn")
            break
        if code != 0:
            ctx.log(
                f"压缩失败 [{EXIT_MEANING.get(code, code)}]: {src.name}\n{out.strip()[-300:]}",
                "error",
            )
            fail += 1
            continue

        arc_path = workdir / arc_name
        good, why = z.verify_archive(arc_path, src.name, size)
        if not good:
            # 7z 对病态文件名会静默产出空包并报成功，这里必须拦下来
            ctx.log(f"打包后核对不通过，已删除坏包，源文件未改动: {src.name} → {why}", "error")
            try:
                arc_path.unlink()
            except OSError:
                pass
            fail += 1
            continue

        if ctx.full_verify:
            v_ok, v_why = z.test_archive(arc_path)
            if not v_ok:
                ctx.log(f"完整性校验失败: {arc_name} → {v_why}", "error")
                fail += 1
                continue

        # 源文件改名 _{原文件名}.{原扩展名}
        renamed = src.name
        try:
            if not src.name.startswith(MARK):
                target = unique_path(src.with_name(marked_name(src.name)))
                src.rename(target)
                renamed = target.name
        except OSError as exc:
            ctx.log(f"压缩成功但源文件改名失败（包已保留）: {src.name} → {exc}", "error")

        touched.add(item.date)
        ok += 1
        ctx.log(f"{arc_name} ← {src.name}（{human_size(size)}，{why}），源文件改名为 {renamed}", "ok")

    if touched:
        ctx.prog(total, total, "正在生成当天目录包…")
        for date in sorted(touched):
            if ctx.cancel.is_set():
                break
            write_manifest(z, workdir, date, ctx)

    ctx.prog(total, total, f"压缩结束：成功 {ok}，失败 {fail}")
    ctx.log(f"压缩结束：成功 {ok}，失败 {fail}", "ok" if fail == 0 else "warn")
    return {"ok": ok, "fail": fail, "total": total}


def extract_job(z: SevenZip, targets: list[Path], outdir: Path, ctx: JobCtx) -> dict:
    """解压到临时目录 → 逐文件加 MARK 前缀搬到输出目录（保留包内层级）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    total = len(targets)
    ok = fail = 0
    ctx.log(f"开始解压 {total} 个包 → {outdir}", "info")

    for n, arc in enumerate(targets, 1):
        if ctx.cancel.is_set():
            ctx.log("已取消", "warn")
            break
        ctx.prog(n - 1, total, f"正在解压 {n}/{total}: {arc.name}")

        if not arc.is_file():
            ctx.log(f"跳过（文件不存在）: {arc}", "warn")
            fail += 1
            continue

        with tempfile.TemporaryDirectory(prefix="zbatch_") as td:
            tmp = Path(td)
            code, out = z.extract_to(arc, tmp)
            if code == RET_CANCELLED:
                ctx.log(f"已取消: {arc.name}", "warn")
                break
            if code != 0:
                ctx.log(
                    f"解压失败 [{EXIT_MEANING.get(code, code)}]: {arc.name}\n{out.strip()[-300:]}",
                    "error",
                )
                fail += 1
                continue

            files = [p for p in tmp.rglob("*") if p.is_file()]
            if not files:
                ctx.log(f"包内没有文件（可能是空包）: {arc.name}", "warn")
                fail += 1
                continue

            moved: list[str] = []
            for f in files:
                rel = f.relative_to(tmp)
                try:
                    dest_dir = outdir / rel.parent
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = unique_path(dest_dir / (MARK + f.name))
                    shutil.move(str(f), str(dest))
                    moved.append(dest.name)
                except OSError as exc:
                    ctx.log(f"写出失败: {f.name} → {exc}", "error")

            note = "（目录文件，不是原始文件）" if RE_INDEX_ARCHIVE.match(arc.name) else ""
            ctx.log(f"{arc.name} → {', '.join(moved) if moved else '无'}{note}", "ok")
            ok += 1

    ctx.prog(total, total, f"解压结束：成功 {ok}，失败 {fail}")
    ctx.log(f"解压结束：成功 {ok}，失败 {fail}，输出到 {outdir}", "ok" if fail == 0 else "warn")
    return {"ok": ok, "fail": fail, "total": total}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def default_workdir() -> Path:
    """
    默认工作目录 = 要归档的那些文件所在的地方。

    目录布局下 zbatch.cmd 在脚本的上一级（脚本自己待在 zbatch/ 子文件夹里），
    所以取上一级；把 zbatch.py 单独拷出去用时，就取脚本自己所在目录。
    """
    if (SCRIPT_DIR.parent / LAUNCHER_NAME).is_file():
        return SCRIPT_DIR.parent
    return SCRIPT_DIR


def load_config() -> dict:
    try:
        return json.loads((SCRIPT_DIR / CONFIG_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        (SCRIPT_DIR / CONFIG_NAME).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------

CHECKED, UNCHECKED = "☑", "☐"
SEL_BG, SEL_FG = "#3478d4", "#ffffff"
FILTER_ALL = "全部"
PICK_HINT = "点一下选中 / 再点取消，Shift+点选中区间"


class MultiPickList(ttk.Frame):
    """
    自己管选中状态的列表：点一下切换、Shift+点选中区间、不需要 Ctrl。

    用带表头的 Treeview 而不是 Listbox，这样「压缩包」和「包内原文件」可以分列，
    不用把文件名挤在同一行里。selectmode="none" 让原生完全不选中，
    于是不存在要和系统选中行为打架的问题，选中外观由自定义 tag 负责。
    选中状态按 payload（这里是 Path）记录而不是按下标，
    这样按天筛选导致列表内容变化时，已选中的项不会丢。
    """

    def __init__(self, master, columns, height=6, remove_on_click=False, on_change=None):
        """columns: [(key, 表头, 宽度, 对齐)]，最后一列自动拉伸。"""
        super().__init__(master)
        self.columns = columns
        self.remove_on_click = remove_on_click
        self.on_change = on_change
        self._items: list[tuple[tuple, object]] = []
        # 选中项用「有序集合」记（dict 当 set 使，保留点选顺序）。
        # 关键：它和当前可见的 items 是两回事——按天筛选只是视图，
        # 被筛掉但已选中的包仍算已选中，否则切一下筛选就丢选择。
        self._sel: dict = {}
        self._anchor = None

        keys = [c[0] for c in columns]
        self.tree = ttk.Treeview(
            self, columns=keys, show="headings", selectmode="none", height=height
        )
        for key, title, width, anchor in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == keys[-1]))
        self.tree.tag_configure("sel", background=SEL_BG, foreground=SEL_FG)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Shift-Button-1>", self._on_click)

    # -- 数据 -------------------------------------------------------------

    def set_items(self, items, keep_selection: bool = True):
        """
        重建列表内容。items 是 [(各列的值, payload)]，值为字符串时按单列处理。
        keep_selection=True：保留选中状态（按天筛选、刷新时用），
                            只丢弃文件已不存在的项。
        keep_selection=False：整体切换了内容（比如模式二换了日期），清空选中。
        """
        self._items = []
        for values, payload in items:
            self._items.append((values if isinstance(values, tuple) else (values,), payload))

        self.tree.delete(*self.tree.get_children())
        for i, (values, _p) in enumerate(self._items):
            self.tree.insert("", "end", iid=str(i), values=values)

        if keep_selection:
            self._sel = {p: None for p in self._sel
                         if not isinstance(p, Path) or p.exists()}
        else:
            self._sel = {}
            self._anchor = None
        self._repaint()

    def items(self):
        return list(self._items)

    def size(self) -> int:
        return len(self._items)

    def selected_payloads(self) -> list:
        return list(self._sel)

    def selected_count(self) -> int:
        return len(self._sel)

    def set_selection(self, payloads):
        self._sel = {p: None for p in payloads}
        self._repaint()

    def clear_selection(self):
        self._sel = {}
        self._anchor = None
        self._repaint()
        if self.on_change:
            self.on_change()

    # -- 内部 -------------------------------------------------------------

    def _index_of(self, payload):
        for i, (_v, p) in enumerate(self._items):
            if p == payload:
                return i
        return None

    def _repaint(self):
        for i, (_v, p) in enumerate(self._items):
            self.tree.item(str(i), tags=("sel",) if p in self._sel else ())

    def _on_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return "break"
        self.click_at(int(iid), bool(event.state & 0x0001))
        return "break"

    def click_at(self, i: int, shift: bool = False):
        """
        点第 i 项。shift=True 时选中锚点到 i 的整个区间（含两端）。
        逻辑单独拆出来，便于脱离鼠标事件直接驱动验证。
        """
        if not (0 <= i < len(self._items)):
            return
        payload = self._items[i][1]

        if shift and self._anchor is not None:
            a = self._index_of(self._anchor)
            if a is not None:
                lo, hi = sorted((a, i))
                for k in range(lo, hi + 1):
                    p = self._items[k][1]
                    if self.remove_on_click:
                        self._sel.pop(p, None)
                    else:
                        self._sel[p] = None
        elif self.remove_on_click or payload in self._sel:
            # remove_on_click 的列表里全是已选中项，点一下就移出
            self._sel.pop(payload, None)
        else:
            self._sel[payload] = None

        self._anchor = payload
        self._repaint()
        if self.on_change:
            self.on_change()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        # 优先用上次的工作目录；它不在了（比如整个项目被挪走）就回到默认位置
        saved = Path(self.cfg.get("workdir") or "")
        self.workdir = saved if saved.is_dir() else default_workdir()

        self.seven_exe = find_7z()
        self.msg_q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_z: SevenZip | None = None

        self.plan: list[PlanItem] = []
        self.checked: list[bool] = []
        self.external_arcs: list[Path] = []
        self.date_by_index: dict[str, dict[int, str]] = {}
        self.all_dates: list[str] = []
        self._cand_all: list[Path] = []
        # 包内文件名缓存：键是 (路径, 大小, mtime)，包一变就自动失效
        self.inner_cache: dict[tuple, str] = {}
        self.s = ui_scale(root)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll)
        self.refresh_all(note=False)
        self._log_header()

    # -- 骨架 -------------------------------------------------------------

    def _build_ui(self):
        self.root.title(APP_TITLE)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(px(1100, self.s), int(sw * 0.92))
        h = min(px(740, self.s), int(sh * 0.90))
        self.root.geometry(self._safe_geometry(self.cfg.get("winsize"), w, h, sw, sh))
        self.root.minsize(min(px(960, self.s), int(sw * 0.7)), min(px(620, self.s), int(sh * 0.6)))

        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)
        self._build_common(outer)

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(8, 0))
        self._build_compress_tab(self.nb)
        self._build_extract_tab(self.nb)
        self._build_log_tab(self.nb)
        self._build_bottom(outer)
        # 切到「批量解压」页时，若已有密码就把包内文件名读出来
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event=None):
        try:
            if self.nb.index("current") == 1:
                self._maybe_scan_inner()
        except tk.TclError:
            pass

    @staticmethod
    def _safe_geometry(saved: str | None, w: int, h: int, sw: int, sh: int) -> str:
        """
        只恢复尺寸，不恢复位置（换个显示器就可能跑到屏幕外）。
        并钳制到屏幕范围内，避免上次在别的 DPI 下存下的尺寸把布局挤坏。
        """
        m = re.match(r"^(\d+)x(\d+)", saved or "")
        if not m:
            return f"{w}x{h}"
        gw = min(max(int(m.group(1)), 640), int(sw * 0.95))
        gh = min(max(int(m.group(2)), 480), int(sh * 0.95))
        return f"{gw}x{gh}"

    def _build_common(self, parent):
        box = ttk.LabelFrame(parent, text="工作目录与密码", padding=8)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="工作目录:").grid(row=0, column=0, sticky="w")
        self.workdir_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.workdir_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(box, text="选择目录…", command=self.pick_workdir).grid(row=0, column=2)
        ttk.Button(box, text="刷新", width=8, command=lambda: self.refresh_all()).grid(
            row=0, column=3, padx=(6, 0)
        )

        ttk.Label(box, text="密码:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(box, textvariable=self.pw_var, show="●")
        self.pw_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        self.pw_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="显示", variable=self.pw_show, command=self._toggle_pw).grid(
            row=1, column=2, pady=(6, 0)
        )
        # 密码填好就顺手把包内文件名读出来（失焦、回车时各试一次）
        self.pw_entry.bind("<FocusOut>", lambda _e: self._maybe_scan_inner())
        self.pw_entry.bind("<Return>", lambda _e: self._maybe_scan_inner())

        ttk.Label(
            box,
            text="始终加密（AES-256 + 加密文件名），密码不落盘，留空无法开始；密码中不能含英文双引号。",
            foreground="#777",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        exe_text = self.seven_exe or "未找到 7z.exe，请确认已加入 PATH 或安装到默认位置"
        ttk.Label(
            box, text=f"7z: {exe_text}", foreground="#777" if self.seven_exe else "#c00"
        ).grid(row=3, column=0, columnspan=4, sticky="w")

    def _toggle_pw(self):
        self.pw_entry.configure(show="" if self.pw_show.get() else "●")

    # -- 压缩页 -----------------------------------------------------------

    def _build_compress_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="批量压缩")

        opts = ttk.Frame(tab)
        opts.pack(fill="x")
        self.include_marked = tk.BooleanVar(value=bool(self.cfg.get("include_marked")))
        self.include_dotted = tk.BooleanVar(value=bool(self.cfg.get("include_dotted")))
        self.full_verify = tk.BooleanVar(value=bool(self.cfg.get("full_verify")))
        ttk.Checkbutton(
            opts,
            text=f"包含 {MARK} 开头的文件（默认跳过上次压缩留下的）",
            variable=self.include_marked,
        ).pack(side="left")
        ttk.Checkbutton(
            opts,
            text="包含点开头的文件（默认跳过 .gitignore 这类项目配置）",
            variable=self.include_dotted,
            command=self.rescan,
        ).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(
            opts,
            text="打包后完整校验（大文件会多读一遍，耗时翻倍）",
            variable=self.full_verify,
        ).pack(side="left", padx=(16, 0))

        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=(6, 4))
        ttk.Button(btns, text="扫描预览", command=self.rescan).pack(side="left")
        ttk.Button(btns, text="开始压缩", command=self.start_compress).pack(side="left", padx=6)
        ttk.Button(btns, text="生成目录包", command=self.start_build_manifests).pack(
            side="left", padx=(6, 0)
        )
        self.lbl_plan = ttk.Label(btns, text="", foreground="#555")
        self.lbl_plan.pack(side="left", padx=10)

        cols = ("chk", "date", "seq", "name", "size", "mtime", "arc")
        heads = {
            "chk": ("压缩", 52, "center", False),
            "date": ("日期", 92, "center", False),
            "seq": ("序号", 50, "center", False),
            "name": ("原文件名", 300, "w", True),
            "size": ("大小", 90, "e", False),
            "mtime": ("修改时间", 152, "center", False),
            "arc": ("将生成的包名", 162, "w", False),
        }
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="none")
        for c in cols:
            text, width, anchor, stretch = heads[c]
            self.tree.heading(c, text=text)
            self.tree.column(c, width=px(width, self.s), minwidth=px(width // 2, self.s),
                             anchor=anchor, stretch=stretch)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)

        ttk.Label(
            tab,
            text="点最左侧「压缩」列可勾掉单个文件。序号按当天内修改时间从老到新排列；"
            "同一天再次运行会接着已有最大序号往后排。\n"
            "「生成目录包」不需要待压缩文件，直接为工作目录里已有的 {YYYYMMDD}{NN}.7z "
            "重建当天的 {YYYYMMDD}index.7z 清单。",
            foreground="#777",
            justify="left",
        ).pack(fill="x", pady=(4, 0))

    def _row_values(self, i: int):
        it = self.plan[i]
        return (
            CHECKED if self.checked[i] else UNCHECKED,
            f"{it.date[:4]}-{it.date[4:6]}-{it.date[6:]}",
            f"{it.seq:02d}",
            it.src.name,
            human_size(it.size),
            fmt_time(it.mtime),
            it.archive_name,
        )

    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        i = int(iid)
        self.checked[i] = not self.checked[i]
        self.tree.item(iid, values=self._row_values(i))
        self._update_plan_summary()

    def _update_plan_summary(self):
        picked = [self.plan[i] for i, c in enumerate(self.checked) if c]
        if not picked:
            self.lbl_plan.configure(text="未勾选任何文件")
            return
        days = sorted({p.date for p in picked})
        total = sum(p.size for p in picked)
        self.lbl_plan.configure(
            text=f"已勾选 {len(picked)} 个文件，{human_size(total)}，涉及 {len(days)} 天"
        )

    def rescan(self):
        files = scan_candidates(
            self.workdir, self.include_marked.get(), self.include_dotted.get()
        )
        seq_by_date, _names, _dates = scan_existing_archives(self.workdir)
        self.plan = build_plan(files, seq_by_date)
        self.checked = [True] * len(self.plan)

        self.tree.delete(*self.tree.get_children())
        for i in range(len(self.plan)):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(i))
        self._update_plan_summary()
        if not self.plan:
            self.lbl_plan.configure(
                text=f"没有需要压缩的文件（.7z、{MARK} 开头的文件、点开头的项目配置文件都已跳过）"
            )

    # -- 解压页 -----------------------------------------------------------

    def _build_extract_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="批量解压")
        s = self.s  # DPI 缩放系数，列表列宽要用

        out_row = ttk.Frame(tab)
        out_row.pack(fill="x", pady=(0, 8))
        ttk.Label(out_row, text="输出目录:").pack(side="left")
        self.outdir_var = tk.StringVar(value=self.cfg.get("outdir") or "")
        ttk.Entry(out_row, textvariable=self.outdir_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(out_row, text="选择…", command=self.pick_outdir).pack(side="left")
        ttk.Button(out_row, text="用工作目录", command=lambda: self.outdir_var.set("")).pack(
            side="left", padx=6
        )
        ttk.Label(
            out_row, text=f"（留空 = 工作目录；解出的文件统一加 {MARK} 前缀）", foreground="#777"
        ).pack(side="left")

        inner_row = ttk.Frame(tab)
        inner_row.pack(fill="x", pady=(0, 8))
        ttk.Button(
            inner_row, text="读取包内文件名", command=lambda: self.start_scan_inner()
        ).pack(side="left")
        self.lbl_inner = ttk.Label(inner_row, text="", foreground="#555")
        self.lbl_inner.pack(side="left", padx=8)
        ttk.Label(
            inner_row,
            text="包内文件名是加密的，输入密码后自动读取一次，结果按包缓存。",
            foreground="#777",
        ).pack(side="left")

        g1 = ttk.LabelFrame(tab, text="模式一 · 按天批量解压", padding=8)
        g1.pack(fill="x")
        ttk.Label(g1, text="日期:").pack(side="left")
        self.day_all_var = tk.StringVar()
        self.day_all_combo = ttk.Combobox(
            g1, textvariable=self.day_all_var, state="readonly", width=14
        )
        self.day_all_combo.pack(side="left", padx=6)
        ttk.Button(g1, text="解压该天全部", command=self.start_extract_day).pack(side="left")
        self.lbl_day_all = ttk.Label(g1, text="", foreground="#555")
        self.lbl_day_all.pack(side="left", padx=10)
        self.day_all_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_day_all_label())

        g2 = ttk.LabelFrame(tab, text="模式二 · 按天按序号解压", padding=8)
        g2.pack(fill="both", expand=True, pady=6)
        top2 = ttk.Frame(g2)
        top2.pack(fill="x")
        ttk.Label(top2, text="日期:").pack(side="left")
        self.day_seq_var = tk.StringVar()
        self.day_seq_combo = ttk.Combobox(
            top2, textvariable=self.day_seq_var, state="readonly", width=14
        )
        self.day_seq_combo.pack(side="left", padx=6)
        ttk.Button(top2, text="解压选中序号", command=self.start_extract_seq).pack(side="left")
        self.lbl_seq = ttk.Label(top2, text="", foreground="#555")
        self.lbl_seq.pack(side="left", padx=10)
        ttk.Label(top2, text=PICK_HINT, foreground="#777").pack(side="left")
        self.seq_list = MultiPickList(
            g2,
            columns=[
                ("seq", "序号", px(64, s), "center"),
                ("arc", "压缩包", px(200, s), "w"),
                ("inner", "包内原文件", px(240, s), "w"),
            ],
            height=6,
            on_change=self._after_seq_change,
        )
        self.seq_list.pack(fill="both", expand=True, pady=(6, 0))
        self.day_seq_combo.bind("<<ComboboxSelected>>", lambda e: self._fill_seq_list())

        g3 = ttk.LabelFrame(tab, text="模式三 · 自由选择解压", padding=8)
        g3.pack(fill="both", expand=True)
        top3 = ttk.Frame(g3)
        top3.pack(fill="x")
        ttk.Button(top3, text="浏览添加 .7z…", command=self.add_external).pack(side="left")
        ttk.Button(top3, text="清空已选", command=self.clear_picked).pack(side="left", padx=6)
        ttk.Button(top3, text="解压选中", command=self.start_extract_free).pack(side="left")
        ttk.Label(top3, text="按天筛选:").pack(side="left", padx=(16, 0))
        self.filter_var = tk.StringVar(value=FILTER_ALL)
        self.filter_combo = ttk.Combobox(
            top3, textvariable=self.filter_var, state="readonly", width=12
        )
        self.filter_combo.pack(side="left", padx=6)
        self.lbl_free = ttk.Label(top3, text="", foreground="#555")
        self.lbl_free.pack(side="left", padx=10)
        self.filter_combo.bind("<<ComboboxSelected>>", lambda e: self._fill_candidates())

        panes = ttk.Frame(g3)
        panes.pack(fill="both", expand=True, pady=(6, 0))
        panes.columnconfigure(0, weight=1, uniform="pane")
        panes.columnconfigure(1, weight=1, uniform="pane")
        panes.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(panes, text=f"可选压缩包　（{PICK_HINT}）", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.cand_list = MultiPickList(
            left,
            columns=[
                ("arc", "压缩包", px(180, s), "w"),
                ("inner", "包内原文件", px(180, s), "w"),
            ],
            height=9,
            on_change=self._after_pick_change,
        )
        self.cand_list.pack(fill="both", expand=True)

        right = ttk.LabelFrame(panes, text="已选中　（点一下移出）", padding=6)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.picked_list = MultiPickList(
            right,
            columns=[
                ("arc", "压缩包", px(180, s), "w"),
                ("inner", "包内原文件", px(180, s), "w"),
            ],
            height=9,
            remove_on_click=True,
            on_change=self._after_picked_change,
        )
        self.picked_list.pack(fill="both", expand=True)

    def pick_outdir(self):
        d = filedialog.askdirectory(title="选择解压输出目录", initialdir=str(self.workdir))
        if d:
            self.outdir_var.set(d)

    def _refresh_day_all_label(self):
        d = date_key(self.day_all_var.get())
        if not d:
            self.lbl_day_all.configure(text="")
            return
        n = len(self.date_by_index.get(d, {}))
        extra = "，含目录包" if (self.workdir / f"{d}index.7z").is_file() else ""
        self.lbl_day_all.configure(text=f"共 {n} 个文件包{extra}")

    def _after_seq_change(self):
        n = self.seq_list.selected_count()
        self.lbl_seq.configure(text=f"已选 {n} 个" if n else "未选")

    def _fill_seq_list(self):
        d = date_key(self.day_seq_var.get())
        items = []
        if d:
            for seq in sorted(self.date_by_index.get(d, {})):
                p = self.workdir / self.date_by_index[d][seq]
                items.append(((f"{seq:02d}", p.name, self._inner_note(p)), p))
            idx = self.workdir / f"{d}index.7z"
            if idx.is_file():
                items.append((("index", idx.name, "← 日期清单"), idx))
        # 换了日期就是换了一批包，选中状态整体清掉
        self.seq_list.set_items(items, keep_selection=False)
        self._after_seq_change()

    # -- 模式三：左右两栏 --------------------------------------------------

    def _all_candidates(self) -> list[Path]:
        """
        左栏的候选包。符合 {YYYYMMDD}{NN}.7z 命名的按日期+序号排在前面，
        其余 .7z（比如随手丢进来的 backup.7z）也一并列出——
        这一栏就是「自由选择」，不该有东西看不到。
        """
        out: list[Path] = []
        _s, names, _d = scan_existing_archives(self.workdir)
        for d in sorted(names):
            for seq in sorted(names[d]):
                out.append(self.workdir / names[d][seq])
            idx = self.workdir / f"{d}index.7z"
            if idx.is_file():
                out.append(idx)
        known = set(out)
        try:
            others = sorted(
                (p for p in self.workdir.iterdir()
                 if p.is_file() and p.suffix.lower() == ".7z" and p not in known),
                key=lambda p: p.name.lower(),
            )
        except OSError:
            others = []
        out.extend(others)
        for p in self.external_arcs:
            if p not in out:
                out.append(p)
        return out

    def _cand_values(self, p: Path) -> tuple:
        if p.parent != self.workdir:
            first = f"[外部] {p}"
        else:
            first = p.name
        return (first, self._inner_note(p))

    def _fill_candidates(self):
        """按「按天筛选」重建左侧列表。选中状态按 Path 记着，换筛选条件不会丢。"""
        self._cand_all = self._all_candidates()
        want = date_key(self.filter_var.get()) if self.filter_var.get() != FILTER_ALL else ""
        items = [
            (self._cand_values(p), p)
            for p in self._cand_all
            if not want or archive_date(p.name) == want
        ]
        self.cand_list.set_items(items)
        self._after_pick_change()

    def add_external(self):
        paths = filedialog.askopenfilenames(
            title="选择要解压的 7z 包",
            filetypes=[("7z 压缩包", "*.7z"), ("所有文件", "*.*")],
        )
        for p in paths:
            path = Path(p)
            if path not in self.external_arcs:
                self.external_arcs.append(path)
        self._fill_candidates()
        self._maybe_scan_inner()

    def _after_pick_change(self):
        picked = self.cand_list.selected_payloads()
        # 右栏按「日期 + 序号」排，比按点选顺序好读
        order = {p: i for i, p in enumerate(self._cand_all)}
        picked.sort(key=lambda p: order.get(p, len(order)))
        self.picked_list.set_items([(self._cand_values(p), p) for p in picked])
        self.picked_list.set_selection(picked)  # 右栏里列出的就是已选中的
        self._update_free_hint()

    def _after_picked_change(self):
        # 在右栏点一下 = 移出该包，同步回左栏的选中状态
        self.cand_list.set_selection(self.picked_list.selected_payloads())
        self._after_pick_change()

    def clear_picked(self):
        self.cand_list.clear_selection()
        self._after_pick_change()

    def _update_free_hint(self):
        picked = self.picked_list.selected_payloads()
        total = 0
        for p in picked:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        self.lbl_free.configure(
            text=f"已选 {len(picked)} 个，{human_size(total)}" if picked else "未选任何包"
        )

    # -- 包内文件名（需要密码，按包缓存）------------------------------------

    def _cache_key(self, p: Path):
        """用「路径 + 大小 + 修改时间」当键，包一旦变动就自动失效。"""
        try:
            st = p.stat()
            return (str(p), st.st_size, int(st.st_mtime))
        except OSError:
            return (str(p), -1, 0)

    def _inner_note(self, p: Path) -> str:
        key = self._cache_key(p)
        if key in self.inner_cache:
            return self.inner_cache[key]
        # 有密码却还没读到 → 先占个位，让用户知道正在读
        return "…" if self.pw_var.get() else ""

    def _inner_pending(self) -> list[Path]:
        return [p for p in self._all_candidates() if self._cache_key(p) not in self.inner_cache]

    def _maybe_scan_inner(self):
        """输入密码后自动读一次；已经在读或已读全了就什么都不做。"""
        if self.busy() or not self.pw_var.get() or not self.seven_exe:
            return
        if self._inner_pending():
            self.start_scan_inner(silent=True)

    def start_scan_inner(self, silent: bool = False):
        pwd = self.pw_var.get()
        if not pwd:
            if not silent:
                messagebox.showinfo(
                    "需要密码", "包内文件名是加密的，输入密码后才能读出来。"
                )
                self.pw_entry.focus_set()
            return
        if self.busy():
            if not silent:
                messagebox.showinfo("请稍候", "已有任务正在运行，请等它结束或先点取消。")
            return
        todo = self._inner_pending()
        if not todo:
            if not silent:
                self.log("包内文件名已是最新，无需重新读取", "info")
                self.refresh_all(note=False)
            return

        self.cancel.clear()
        self.active_z = SevenZip(self.seven_exe, pwd, self.cancel)
        self.btn_cancel.configure(state="normal")
        self.pbar.configure(maximum=len(todo), value=0)
        self.status_var.set("正在读取包内文件名…")
        self.log(f"开始读取包内文件名（待补 {len(todo)} 个包）", "info")

        workdir = self.workdir

        def runner():
            z = self.active_z
            covered: set[Path] = set()
            from_manifest = 0
            done = 0

            # 第一步：按天读目录包。一次调用就能拿到当天所有包的文件名，
            # 比逐个读包快一个数量级。这是清单本来就有的用途。
            by_day: dict[str, list[Path]] = {}
            for p in todo:
                d = archive_date(p.name)
                if d:
                    by_day.setdefault(d, []).append(p)

            for d in sorted(by_day):
                if self.cancel.is_set():
                    break
                mapping = read_day_manifest(z, workdir, d)
                if not mapping:
                    continue
                for p in by_day[d]:
                    hit = mapping.get(p.name)
                    if not hit:
                        continue
                    names, packed = hit
                    try:
                        now = fmt_time(p.stat().st_mtime)
                    except OSError:
                        continue
                    # 清单是打包那一刻的快照。包在那之后被改动过（打包时间对不上），
                    # 清单里的名字就不能信了，退回逐个读。
                    if packed and packed != now:
                        continue
                    self.msg_q.put(
                        ("inner", self._cache_key(p), summarize_names(names) or "（清单未记录）")
                    )
                    covered.add(p)
                    from_manifest += 1
                done += 1
                self.msg_q.put(("prog", done, len(todo), f"已从 {d}index.7z 读取当天清单"))

            # 第二步：清单覆盖不到的（当天没有清单、清单里没有、或清单已过期）逐个读
            rest = [p for p in todo if p not in covered]
            if rest:
                self.msg_q.put(("log", "info", f"另有 {len(rest)} 个包不在清单覆盖范围内，逐个读取"))
            for i, p in enumerate(rest):
                if self.cancel.is_set():
                    self.msg_q.put(("log", "warn", "已取消读取包内文件名"))
                    break
                self.msg_q.put(
                    ("prog", done, len(todo), f"正在逐个读取 {i + 1}/{len(rest)}: {p.name}")
                )
                self.msg_q.put(("inner", self._cache_key(p), inner_names(z, p)))
                done += 1

            self.msg_q.put(
                ("log", "ok",
                 f"包内文件名读取完成：清单提供 {from_manifest} 个，逐个读取 {len(rest)} 个")
            )
            self.msg_q.put(("prog", len(todo), len(todo), "包内文件名读取完成"))
            self.msg_q.put(("busy", False))

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    # -- 日志页 -----------------------------------------------------------

    def _build_log_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="日志")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Button(top, text="导出日志…", command=self.export_log).pack(side="left")
        ttk.Button(top, text="清空", command=self.clear_log).pack(side="left", padx=6)

        self.log_text = scrolledtext.ScrolledText(
            tab, wrap="word", height=20, font=("Consolas", 9)
        )
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))
        for tag, color in (
            ("error", "#c00"),
            ("warn", "#b60"),
            ("ok", "#070"),
            ("info", "#222"),
        ):
            self.log_text.tag_configure(tag, foreground=color)

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def export_log(self):
        p = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            initialfile=f"zbatch-log-{date_of(datetime.now().timestamp())}.txt",
            filetypes=[("文本文件", "*.txt")],
        )
        if not p:
            return
        try:
            Path(p).write_text(self.log_text.get("1.0", tk.END), encoding="utf-8-sig")
            messagebox.showinfo("已导出", f"日志已保存到:\n{p}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    def log(self, text: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] {text}\n", level)
        self.log_text.see(tk.END)

    def _log_header(self):
        self.log(f"{APP_TITLE} 已启动", "ok")
        self.log(f"工作目录: {self.workdir}")
        self.log(f"7z 路径: {self.seven_exe or '未找到'}", "ok" if self.seven_exe else "error")
        self.log(
            "命名规则: 文件包 {YYYYMMDD}{NN}.7z ｜ 目录包 {YYYYMMDD}index.7z ｜ "
            f"压缩后源文件与解压产物统一加 {MARK} 前缀"
        )

    # -- 底部状态条 -------------------------------------------------------

    def _build_bottom(self, parent):
        bar = ttk.Frame(parent, padding=(0, 8, 0, 0))
        bar.pack(fill="x")
        self.pbar = ttk.Progressbar(bar, mode="determinate", length=px(260, self.s))
        self.pbar.pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=10)
        ttk.Button(bar, text=f"一键删除 {MARK} 文件", command=self.start_delete).pack(side="right")
        self.btn_cancel = ttk.Button(
            bar, text="取消当前任务", command=self.request_cancel, state="disabled"
        )
        self.btn_cancel.pack(side="right", padx=8)

    # -- 线程与消息 -------------------------------------------------------

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _guard(self) -> bool:
        if not self.seven_exe:
            messagebox.showerror(
                "找不到 7z",
                "未找到 7z.exe。请确认 7-Zip 已安装并加入 PATH，\n"
                "或安装到 C:\\Program Files\\7-Zip\\ 后重启本工具。",
            )
            return False
        if self.busy():
            messagebox.showinfo("请稍候", "已有任务正在运行，请等它结束或先点取消。")
            return False
        return True

    def _ask_password(self) -> str | None:
        pw = self.pw_var.get()
        if not pw:
            messagebox.showwarning(
                "需要密码",
                "本工具始终以 AES-256 + 加密文件名的方式打包，密码不能为空。\n"
                "空密码时 7z 根本不会加密，所以这里不允许继续。",
            )
            self.pw_entry.focus_set()
            return None
        if '"' in pw:
            messagebox.showerror(
                "密码含有不支持的字符",
                '7-Zip 的命令行不允许密码中出现英文双引号 "，含该字符时 7z 会直接报命令行错误。\n'
                "请改用其他密码。",
            )
            self.pw_entry.focus_set()
            return None
        return pw

    def request_cancel(self):
        if self.busy():
            self.cancel.set()
            if self.active_z:
                self.active_z.kill()
            self.log("已请求取消，正在停止…", "warn")

    def _start(self, job: Callable[[SevenZip, JobCtx], None], label: str):
        if not self._guard():
            return
        pwd = self._ask_password()
        if pwd is None:
            return
        self.cancel.clear()
        self.active_z = SevenZip(self.seven_exe, pwd, self.cancel)
        self.btn_cancel.configure(state="normal")
        self.pbar.configure(value=0, maximum=100)
        self.status_var.set(label)

        ctx = JobCtx(
            log=self.lg,
            prog=self.prog,
            cancel=self.cancel,
            full_verify=self.full_verify.get(),
        )
        z = self.active_z

        def runner():
            try:
                job(z, ctx)
            except Exception as exc:  # 兜底，避免线程静默死掉
                self.msg_q.put(("log", "error", f"任务异常终止: {exc!r}"))
            finally:
                self.msg_q.put(("log", "info", "_" * 64))
                self.msg_q.put(("busy", False))

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    def _poll(self):
        try:
            while True:
                msg = self.msg_q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log(msg[2], msg[1])
                elif kind == "prog":
                    _k, done, total, text = msg
                    self.pbar.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(text)
                elif kind == "inner":
                    # 读到一个就记一个；界面等本轮结束后统一刷新
                    _k, key, text = msg
                    self.inner_cache[key] = text
                elif kind == "busy":
                    self.btn_cancel.configure(state="disabled")
                    self.active_z = None
                    self.status_var.set("就绪")
                    self.refresh_all(note=False)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def lg(self, text, level="info"):
        self.msg_q.put(("log", level, text))

    def prog(self, done, total, text):
        self.msg_q.put(("prog", done, total, text))

    # -- 压缩 -------------------------------------------------------------

    def start_compress(self):
        picked = [self.plan[i] for i, c in enumerate(self.checked) if c]
        if not picked:
            messagebox.showinfo("没有可压缩的文件", "请先点「扫描预览」，并至少勾选一个文件。")
            return
        workdir = self.workdir
        self._start(
            lambda z, ctx: compress_job(z, workdir, picked, ctx),
            f"正在压缩 {len(picked)} 个文件…",
        )

    def start_build_manifests(self):
        """为已有压缩包重建当天目录包，不需要扫描到的待压缩文件。"""
        workdir = self.workdir
        self._start(
            lambda z, ctx: build_manifests_job(z, workdir, ctx),
            "正在生成目录包…",
        )

    # -- 解压 -------------------------------------------------------------

    def _outdir(self) -> Path:
        raw = self.outdir_var.get().strip()
        if not raw:
            return self.workdir
        p = Path(raw)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("输出目录不可用", str(exc))
        return p

    def start_extract_day(self):
        label = self.day_all_var.get()
        d = date_key(label)
        if not d:
            messagebox.showinfo("请选择日期", "工作目录里没有扫描到任何压缩包。")
            return
        _s, names, _d = scan_existing_archives(self.workdir)
        targets = [self.workdir / names[d][s] for s in sorted(names.get(d, {}))]
        idx = self.workdir / f"{d}index.7z"
        if idx.is_file():
            targets.append(idx)
        if not targets:
            messagebox.showinfo("没有可用包", f"{label} 当天没有可用压缩包。")
            return
        outdir = self._outdir()
        self._start(lambda z, ctx: extract_job(z, targets, outdir, ctx), f"按天解压 {label}…")

    def start_extract_seq(self):
        targets = self.seq_list.selected_payloads()
        if not targets:
            messagebox.showinfo("请选择序号", f"请在列表里点选要解压的包。\n{PICK_HINT}")
            return
        outdir = self._outdir()
        self._start(
            lambda z, ctx: extract_job(z, targets, outdir, ctx),
            f"解压选中的 {len(targets)} 个包…",
        )

    def start_extract_free(self):
        targets = self.picked_list.selected_payloads()
        if not targets:
            messagebox.showinfo("请选择包", f"请在左侧「可选压缩包」里点选。\n{PICK_HINT}")
            return
        outdir = self._outdir()
        self._start(
            lambda z, ctx: extract_job(z, targets, outdir, ctx),
            f"解压选中的 {len(targets)} 个包…",
        )

    # -- 一键删除 ---------------------------------------------------------

    def start_delete(self):
        # 删 _ 文件跟 7z 无关，所以不走 _guard()（那个会要求先找到 7z.exe）
        if self.busy():
            messagebox.showinfo("请稍候", "已有任务正在运行，请等它结束或先点取消。")
            return
        targets = collect_marked(self.workdir)
        if not targets:
            messagebox.showinfo(
                "没有可删除的项", f"工作目录下没有 {MARK} 开头的文件或文件夹。"
            )
            return
        total_size = sum(path_size(p) for p in targets)
        preview = "\n".join(f"  {p.name}" for p in targets[:15])
        more = f"\n  …以及另外 {len(targets) - 15} 项" if len(targets) > 15 else ""
        if not messagebox.askyesno(
            "确认删除",
            f"将在以下目录删除 {len(targets)} 项（共 {human_size(total_size)}）：\n"
            f"{self.workdir}\n\n{preview}{more}\n\n"
            f"只删 {MARK} 开头的文件和文件夹，不会碰 .7z 和工具自身。\n确定继续吗？",
            icon="warning",
        ):
            self.log("已取消一键删除", "info")
            return

        self.pbar.configure(maximum=len(targets), value=0)
        done = ok = 0
        for p in targets:
            try:
                remove_path(p)
                ok += 1
                self.log(f"已删除: {p.name}", "ok")
            except OSError as exc:
                self.log(f"删除失败: {p.name} → {exc}", "error")
            done += 1
            self.pbar.configure(value=done)
            self.status_var.set(f"删除中 {done}/{len(targets)}")
        self.log(
            f"一键删除完成：成功 {ok}，失败 {len(targets) - ok}，释放 {human_size(total_size)}",
            "ok",
        )
        self.status_var.set("就绪")
        self.refresh_all(note=False)

    # -- 刷新 -------------------------------------------------------------

    def pick_workdir(self):
        d = filedialog.askdirectory(title="选择工作目录", initialdir=str(self.workdir))
        if not d:
            return
        self.workdir = Path(d)
        self.external_arcs.clear()
        self.refresh_all()

    def refresh_all(self, note: bool = True):
        self.workdir_var.set(str(self.workdir))
        _s, self.date_by_index, dates = scan_existing_archives(self.workdir)
        self.all_dates = sorted(dates, reverse=True)
        labels = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in self.all_dates]

        for combo, var in (
            (self.day_all_combo, self.day_all_var),
            (self.day_seq_combo, self.day_seq_var),
        ):
            combo.configure(values=labels)
            if var.get() not in labels:
                var.set(labels[0] if labels else "")

        filter_labels = [FILTER_ALL] + labels
        self.filter_combo.configure(values=filter_labels)
        if self.filter_var.get() not in filter_labels:
            self.filter_var.set(FILTER_ALL)

        self._fill_seq_list()
        self._fill_candidates()
        self._refresh_day_all_label()
        self._refresh_inner_label()
        self.rescan()
        if note:
            self.log(f"已切换到工作目录: {self.workdir}", "info")

    def _refresh_inner_label(self):
        cands = self._all_candidates()
        if not cands:
            self.lbl_inner.configure(text="工作目录里没有压缩包")
        elif not self.pw_var.get():
            self.lbl_inner.configure(text="输入密码后自动读取")
        else:
            done = len(cands) - len(self._inner_pending())
            self.lbl_inner.configure(text=f"已读取 {done}/{len(cands)} 个包")

    # -- 退出 -------------------------------------------------------------

    def _on_close(self):
        if self.busy():
            if not messagebox.askyesno("任务进行中", "有任务正在运行，确定要退出吗？"):
                return
            self.cancel.set()
            if self.active_z:
                self.active_z.kill()
            self.worker.join(timeout=3)
        save_config(
            {
                "workdir": str(self.workdir),
                "outdir": self.outdir_var.get(),
                "winsize": f"{self.root.winfo_width()}x{self.root.winfo_height()}",
                "include_marked": self.include_marked.get(),
                "include_dotted": self.include_dotted.get(),
                "full_verify": self.full_verify.get(),
            }
        )
        self.root.destroy()


def main():
    if not sys.platform.startswith("win"):
        print("本工具依赖 Windows 版 7-Zip，仅支持 Windows。", file=sys.stderr)
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)  # 高 DPI 下字体不发虚
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
