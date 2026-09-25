# -*- coding: utf-8 -*-
"""
zbatch.py 的端到端自测：造真实数据跑完整闭环（压缩 → 二次压缩 → 生成目录包 →
一键删除 → 解压），全程在 %TEMP% 下进行，不碰工作目录里的真实文件。

用法： python zbatch_selftest.py
需要先安装 7-Zip。逐条打印断言结果，有失败则退出码非 0。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("zbatch.py")
ROOT = Path(os.environ["TEMP"]) / "zbatch_e2e" / "scratch"
PWD = "e2e-P@ss word&中文"

spec = importlib.util.spec_from_file_location("zbatch", SCRIPT)
zb = importlib.util.module_from_spec(spec)
sys.modules["zbatch"] = zb  # @dataclass 需要模块在 sys.modules 里
spec.loader.exec_module(zb)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(folder, name, body, when):
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    ts = datetime(*when).timestamp()
    os.utime(p, (ts, ts))
    return p


class Z:
    inst = None


def new_ctx(full_verify=False):
    logs = []
    return zb.JobCtx(log=lambda t, l="info": logs.append((l, t)),
                     prog=lambda *a: None, cancel=threading.Event(),
                     full_verify=full_verify), logs


def unpack_manifest(arc):
    with tempfile.TemporaryDirectory() as td:
        code, _ = Z.inst.extract_to(arc, Path(td))
        txts = list(Path(td).rglob("*.txt"))
        return code, (txts[0].read_text(encoding="utf-8-sig") if txts else "")


def compress(folder):
    seqs, _, _ = zb.scan_existing_archives(folder)
    plan = zb.build_plan(zb.scan_candidates(folder, False), seqs)
    c, logs = new_ctx()
    return plan, zb.compress_job(Z.inst, folder, plan, c), logs


def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    WORK = ROOT / "work"
    WORK.mkdir(parents=True)

    exe = zb.find_7z()
    check("找到 7z.exe", bool(exe), str(exe))
    if not exe:
        return 1
    Z.inst = zb.SevenZip(exe, PWD)

    # ---------------------------------------------------------------- 扫描/编号
    print("\n== 扫描与分天编号 ==")
    make(WORK, "日报 1.txt", "day1 file 1\n", (2026, 9, 23, 8, 0, 0))
    make(WORK, "日报 2.txt", "day1 file 2\n", (2026, 9, 23, 9, 30, 0))
    make(WORK, "图 片.png", "day2 file 1\n", (2026, 9, 25, 7, 0, 0))
    make(WORK, "notes.md", "day2 file 2\n", (2026, 9, 25, 10, 0, 0))
    (WORK / "old.7z").write_bytes(b"not really an archive")
    make(WORK, "_已压缩过.txt", "skipped\n", (2026, 9, 23, 11, 0, 0))
    make(WORK, "子目录/内层.txt", "nested\n", (2026, 9, 23, 12, 0, 0))

    got = sorted(f.name for f in zb.scan_candidates(WORK, False))
    check("只挑顶层非 7z 文件（4 个）", len(got) == 4, str(got))
    check("跳过 _ 开头文件", "_已压缩过.txt" not in got, str(got))
    check("不递归子目录", "内层.txt" not in got, str(got))
    seqs, _, _ = zb.scan_existing_archives(WORK)
    naming = {p.src.name: p.archive_name for p in zb.build_plan(zb.scan_candidates(WORK, False), seqs)}
    check("最老的文件是 01", naming.get("日报 1.txt") == "2026092301.7z", str(naming))
    check("次老的是 02", naming.get("日报 2.txt") == "2026092302.7z", str(naming))
    check("另一天独立从 01 起", naming.get("图 片.png") == "2026092501.7z", str(naming))
    check("另一天第二个是 02", naming.get("notes.md") == "2026092502.7z", str(naming))

    # ---------------------------------------------------------------- 压缩
    print("\n== 首次压缩 ==")
    _p, res, logs = compress(WORK)
    check("4 个全部成功", res == {"ok": 4, "fail": 0, "total": 4}, str(res))
    for want in ("2026092301.7z", "2026092302.7z", "2026092501.7z",
                 "2026092502.7z", "20260923index.7z", "20260925index.7z"):
        check(f"生成 {want}", (WORK / want).is_file())
    check("源文件改名为 _日报 1.txt", (WORK / "_日报 1.txt").is_file())
    check("原名文件已不在", not (WORK / "日报 1.txt").exists())
    check("子目录未被碰", (WORK / "子目录" / "内层.txt").is_file())
    check("外来 old.7z 原样保留", (WORK / "old.7z").read_bytes() == b"not really an archive")

    print("\n== 包内容与加密 ==")
    code, entries = Z.inst.list_entries(WORK / "2026092301.7z")
    check("读到 1 个条目", code == 0 and len(entries) == 1, f"code={code} n={len(entries)}")
    check("包内文件名正确", entries and entries[0].get("Path") == "日报 1.txt")
    check("包内文件已加密", entries and entries[0].get("Encrypted") == "+")
    check("方法为 Copy(仅存储)+AES", entries and "Copy" in entries[0].get("Method", ""))
    check("错误密码读不出包（表头已加密）",
          zb.SevenZip(exe, "nope").list_entries(WORK / "2026092301.7z")[0] != 0)

    print("\n== 目录包内容 ==")
    code, body = unpack_manifest(WORK / "20260923index.7z")
    check("目录包解出清单", code == 0 and body, f"code={code}")
    check("清单列了两个包", "2026092301.7z" in body and "2026092302.7z" in body)
    check("清单记录了原文件名", "日报 1.txt" in body and "日报 2.txt" in body)
    check("清单合计 2 个压缩包", "合计: 2 个压缩包" in body)
    rows = [l for l in body.splitlines() if l[:1].isdigit()]
    check("清单表格行里不含目录包自身", not any("index" in r for r in rows), str(rows))

    # ---------------------------------------------------------------- 同天二次运行
    print("\n== 同一天二次运行 ==")
    make(WORK, "日报 3.txt", "day1 file 3 late\n", (2026, 9, 23, 15, 0, 0))
    names_now = sorted(f.name for f in zb.scan_candidates(WORK, False))
    check("二次扫描只剩新文件", names_now == ["日报 3.txt"], str(names_now))
    seqs, _, _ = zb.scan_existing_archives(WORK)
    plan = zb.build_plan(zb.scan_candidates(WORK, False), seqs)
    check("序号顺延到 03，不重排已有", plan and plan[0].archive_name == "2026092303.7z",
          str([p.archive_name for p in plan]))
    _p, res2, _l = compress(WORK)
    check("二次压缩成功 1 个", res2 == {"ok": 1, "fail": 0, "total": 1}, str(res2))
    check("01 包未被覆盖", Z.inst.list_entries(WORK / "2026092301.7z")[1][0]["Path"] == "日报 1.txt")
    _c, body = unpack_manifest(WORK / "20260923index.7z")
    check("目录包更新为全量 3 条", "2026092303.7z" in body and "合计: 3 个压缩包" in body)
    check("清单保留原有 01/02", "2026092301.7z" in body and "2026092302.7z" in body)

    print("\n== 空转运行 ==")
    seqs, names, dates = zb.scan_existing_archives(WORK)
    check("日期集合含两天", "20260923" in dates and "20260925" in dates, str(sorted(dates)))
    check("序号 01/02/03 齐全", sorted(names["20260923"]) == [1, 2, 3], str(sorted(names["20260923"])))
    check("无新文件时计划为空", zb.build_plan(zb.scan_candidates(WORK, False), seqs) == [])

    # ---------------------------------------------------------------- 一键删除
    print("\n== 一键删除范围 ==")
    targets = zb.collect_marked(WORK)
    tnames = sorted(p.name for p in targets)
    check("收集到 6 个 _ 目标", len(targets) == 6, str(tnames))
    check("不含任何 .7z", not any(t.suffix == ".7z" for t in targets), str(tnames))
    check("不含非 _ 开头的子目录", "子目录" not in tnames, str(tnames))
    check("不含工具自身", not any(t.name in zb.SELF_NAMES for t in targets))
    total_size = sum(zb.path_size(t) for t in targets)
    for t in targets:
        zb.remove_path(t)
    check("删除后 _ 目标清空", zb.collect_marked(WORK) == [])
    check(f"统计到大小时不为 0（{zb.human_size(total_size)}）", total_size > 0)
    check("压缩包全部保留", len(list(WORK.glob("*.7z"))) == 8, str(len(list(WORK.glob("*.7z")))))

    # ---------------------------------------------------------------- 解压
    print("\n== 解压（三种模式共用同一个 job）==")
    outdir = ROOT / "restored"
    _s, names, _d = zb.scan_existing_archives(WORK)
    tg = [WORK / names["20260923"][s] for s in sorted(names["20260923"])]
    tg.append(WORK / "20260923index.7z")
    c, _l = new_ctx()
    res3 = zb.extract_job(Z.inst, tg, outdir, c)
    check("4 个包解压成功", res3 == {"ok": 4, "fail": 0, "total": 4}, str(res3))
    check("解出的文件带 _ 前缀", (outdir / "_日报 1.txt").is_file())
    check("内容与原始一致", (outdir / "_日报 1.txt").read_text(encoding="utf-8") == "day1 file 1\n")
    check("共 4 项（3 文件 + 目录）", len(list(outdir.iterdir())) == 4)
    check("目录文件也带 _ 前缀", (outdir / "_20260923index.txt").is_file())
    c, _l = new_ctx()
    zb.extract_job(Z.inst, [WORK / names["20260923"][1]], outdir, c)
    check("重名自动 -2 不覆盖", (outdir / "_日报 1-2.txt").is_file())

    print("\n== 解压时是否加 _ 前缀（mark 开关，默认开）==")
    plain = ROOT / "plain-out"
    c, _l = new_ctx()
    res = zb.extract_job(Z.inst, [WORK / names["20260923"][1]], plain, c, mark=False)
    check("mark=False 解压成功", res == {"ok": 1, "fail": 0, "total": 1}, str(res))
    check("mark=False 还原成包内原始文件名", (plain / "日报 1.txt").is_file(),
          str(sorted(p.name for p in plain.iterdir())))
    check("mark=False 时不带 _ 前缀",
          not any(p.name.startswith("_") for p in plain.iterdir()),
          str(sorted(p.name for p in plain.iterdir())))
    check("mark=False 内容正确",
          (plain / "日报 1.txt").read_text(encoding="utf-8") == "day1 file 1\n")
    c, _l = new_ctx()
    zb.extract_job(Z.inst, [WORK / names["20260923"][1]], plain, c, mark=False)
    check("mark=False 重名同样 -2，不覆盖", (plain / "日报 1-2.txt").is_file(),
          str(sorted(p.name for p in plain.iterdir())))

    print("\n== 包内带子目录的解压 ==")
    nest = ROOT / "nest"
    make(nest, "层1/内层文件.txt", "deep\n", (2026, 9, 24, 10, 0, 0))
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", f"-p{PWD}"], ["nested.7z", "层1"], cwd=nest)
    nout = ROOT / "nest-out"
    c, _l = new_ctx()
    res = zb.extract_job(Z.inst, [nest / "nested.7z"], nout, c)
    check("解压成功", res == {"ok": 1, "fail": 0, "total": 1}, str(res))
    check("层级被保留", (nout / "层1" / "_内层文件.txt").is_file(),
          str(sorted(p.as_posix() for p in nout.rglob("*"))))

    # ---------------------------------------------------------------- 界面日期接线
    print("\n== 界面日期接线（下拉显示 2026-09-25，内部键 20260925）==")
    _s, names, dates = zb.scan_existing_archives(WORK)
    check("date_key 能把标签还原成键",
          all(zb.date_key(f"{d[:4]}-{d[4:6]}-{d[6:]}") == d for d in dates), str(sorted(dates)))
    check("date_key 对空值不炸", zb.date_key("") == "" and zb.date_key(None) == "")
    day = sorted(dates)[0]
    d2 = zb.date_key(f"{day[:4]}-{day[4:6]}-{day[6:]}")
    picked = [WORK / names[d2][s] for s in sorted(names.get(d2, {}))] + [WORK / f"{d2}index.7z"]
    check("按天下拉能选到包（不是 0 个）", len(picked) > 1, f"picked={len(picked)}")
    check("选出的包都真实存在", all(p.is_file() for p in picked))

    # ---------------------------------------------------------------- 0 字节
    print("\n== 0 字节文件（7z 对空文件不设 Encrypted 标记）==")
    sub = ROOT / "empty"
    sub.mkdir(parents=True)
    (sub / "空文件.txt").write_bytes(b"")
    (sub / "有内容.txt").write_bytes(b"data\n")
    _p, res, _l = compress(sub)
    check("两个都成功", res == {"ok": 2, "fail": 0, "total": 2}, str(res))
    check("0 字节源文件也改名", (sub / "_空文件.txt").is_file())
    packed = {}
    for a in sorted(sub.glob("*.7z")):
        for e in Z.inst.list_entries(a)[1]:
            packed[e["Path"]] = (e["Size"], e.get("Encrypted"), a)
    check("0 字节文件 Size=0", packed.get("空文件.txt", ("",))[0] == "0", str(packed))
    check("有内容文件 Size=5", packed.get("有内容.txt", ("",))[0] == "5", str(packed))
    check("有内容条目带加密标记", packed.get("有内容.txt", ("", "-"))[1] == "+")
    empty_arc = packed["空文件.txt"][2]
    check("0 字节包表头仍加密", zb.SevenZip(exe, "nope").list_entries(empty_arc)[0] != 0)

    # ---------------------------------------------------------------- 生成目录包（新）
    print("\n== 生成目录包：只有现成压缩包的文件夹 ==")
    only = ROOT / "only-archives"
    only.mkdir(parents=True)
    # 三个正常包（两个日期）+ 一个装了多个文件的包 + 一个带层级的包
    make(only, "s1.txt", "s1\n", (2026, 9, 20, 8, 0, 0))
    make(only, "s2.txt", "s2\n", (2026, 9, 20, 9, 0, 0))
    make(only, "s3.txt", "s3\n", (2026, 9, 21, 8, 0, 0))
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", f"-p{PWD}"], ["2026092001.7z", "s1.txt"], cwd=only)
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", f"-p{PWD}"], ["2026092002.7z", "s2.txt"], cwd=only)
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", f"-p{PWD}"], ["2026092101.7z", "s3.txt"], cwd=only)
    make(only, "x1.txt", "x1\n", (2026, 9, 21, 9, 0, 0))
    make(only, "x2.txt", "x2\n", (2026, 9, 21, 9, 0, 0))
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", f"-p{PWD}"],
               ["2026092102.7z", "x1.txt", "x2.txt"], cwd=only)
    for leftover in ("s1.txt", "s2.txt", "s3.txt", "x1.txt", "x2.txt"):
        (only / leftover).unlink()
    check("此时文件夹里只剩压缩包",
          not any(p for p in only.iterdir() if p.suffix != ".7z"),
          str(sorted(p.name for p in only.iterdir())))
    check("还没有任何目录包", not list(only.glob("*index.7z")))

    c, logs = new_ctx()
    res = zb.build_manifests_job(Z.inst, only, c)
    check("为 2 天各生成一个目录包", res == {"ok": 2, "fail": 0, "total": 2}, str(res))
    check("生成 20260920index.7z", (only / "20260920index.7z").is_file())
    check("生成 20260921index.7z", (only / "20260921index.7z").is_file())
    _c, b20 = unpack_manifest(only / "20260920index.7z")
    check("20 日清单含 2 个包", "2026092001.7z" in b20 and "2026092002.7z" in b20)
    check("20 日清单含原文件名", "s1.txt" in b20 and "s2.txt" in b20)
    _c, b21 = unpack_manifest(only / "20260921index.7z")
    check("21 日清单含 2 个包", "2026092101.7z" in b21 and "2026092102.7z" in b21)
    check("多文件包被如实记下（不再跳过）", "x1.txt、x2.txt" in b21, b21[:400])
    check("多文件包大小取合计", "合计: 2 个压缩包" in b21)

    print("\n== 生成目录包：带层级的包 ==")
    lay = ROOT / "layered"
    make(lay, "层1/a.txt", "a\n", (2026, 9, 22, 8, 0, 0))
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", f"-p{PWD}"], ["2026092201.7z", "层1"], cwd=lay)
    shutil.rmtree(lay / "层1")
    c, _l = new_ctx()
    zb.build_manifests_job(Z.inst, lay, c)
    _c, bl = unpack_manifest(lay / "20260922index.7z")
    check("嵌套包记录了相对路径", "层1/a.txt" in bl, bl[:400])
    check("目录条目没有被当成文件", "层1、" not in bl, bl[:400])

    print("\n== 生成目录包：没有可汇总的东西 ==")
    bare = ROOT / "bare"
    bare.mkdir(parents=True)
    c, logs = new_ctx()
    res = zb.build_manifests_job(Z.inst, bare, c)
    check("返回 0 个", res == {"ok": 0, "fail": 0, "total": 0}, str(res))
    check("给出可读提示", any("无从生成目录" in t for _l, t in logs),
          str([t for _l, t in logs]))

    print("\n== 生成目录包：重跑原地更新（不新增文件）==")
    before = sorted(p.name for p in only.glob("*.7z"))
    c, _l = new_ctx()
    zb.build_manifests_job(Z.inst, only, c)
    after = sorted(p.name for p in only.glob("*.7z"))
    check("包列表不变（目录包原地覆盖）", before == after, f"{before} -> {after}")
    check("只有 2 个目录包", len(list(only.glob("*index.7z"))) == 2)

    print("\n== 生成目录包：密码不对时优雅跳过 ==")
    wrong = ROOT / "wrongpw"
    wrong.mkdir(parents=True)
    make(wrong, "s1.txt", "s1\n", (2026, 9, 23, 8, 0, 0))
    # 01 用另一个密码打包（模拟密码未知的存量包），02 用当前密码
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", "-p别的密码"], ["2026092301.7z", "s1.txt"],
               cwd=wrong)
    Z.inst.run(["a", "-t7z", "-mx=0", "-mhe=on", f"-p{PWD}"], ["2026092302.7z", "s1.txt"],
               cwd=wrong)
    (wrong / "s1.txt").unlink()
    c, logs = new_ctx()
    res = zb.build_manifests_job(Z.inst, wrong, c)
    check("仍然完成（读不出的包被跳过）", res == {"ok": 1, "fail": 0, "total": 1}, str(res))
    check("给出了读不出的警告", any("读不出内容" in t for _l, t in logs),
          str([t for _l, t in logs]))
    _c, bw = unpack_manifest(wrong / "20260923index.7z")
    check("清单只列能读到的包", "2026092302.7z" in bw and "2026092301.7z" not in bw, bw[:400])

    # ---------------------------------------------------------------- 选中语义
    print("\n== 选中语义（点一下切换 / Shift 选区间 / 不需要 Ctrl）==")
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        cols = [("c1", "第一列", 160, "w"), ("c2", "第二列", 160, "w")]
        fired = []
        mpl = zb.MultiPickList(root, columns=cols, height=5, on_change=lambda: fired.append(1))
        fake = [ROOT / f"pick{i}.7z" for i in range(5)]
        for f in fake:
            f.write_bytes(b"x")
        full = [((f"item{i}", f"内含{i}.txt"), p) for i, p in enumerate(fake)]
        mpl.set_items(full)
        check("两列内容都进了列表", mpl.items()[0][0] == ("item0", "内含0.txt"), str(mpl.items()[0]))

        mpl.click_at(0)
        check("点一下 = 选中", mpl.selected_payloads() == [fake[0]], str(mpl.selected_payloads()))
        mpl.click_at(0)
        check("再点一下 = 取消", mpl.selected_count() == 0)
        mpl.click_at(1)
        mpl.click_at(3)
        check("不相邻的两项可各自选中（无需 Ctrl）",
              sorted(mpl.selected_payloads()) == [fake[1], fake[3]], str(mpl.selected_payloads()))

        mpl.click_at(0, shift=True)
        check("Shift+点 = 追加锚点到该点的区间（含两端，不清掉原有选中）",
              sorted(mpl.selected_payloads()) == fake[0:4], str(mpl.selected_payloads()))
        mpl.click_at(4, shift=True)
        check("Shift 继续扩展区间", sorted(mpl.selected_payloads()) == fake, str(mpl.selected_payloads()))

        mpl.clear_selection()
        check("清空后锚点也重置", mpl.selected_count() == 0 and mpl._anchor is None)
        mpl.click_at(4)
        mpl.click_at(2, shift=True)
        check("反向 Shift 选 2..4",
              sorted(mpl.selected_payloads()) == fake[2:5], str(mpl.selected_payloads()))

        # 按天筛选只是视图：可见项变少，已选中的不能跟着丢
        mpl.set_items(full[:2])
        check("列表内容变少后已选中不丢（筛选只是视图）",
              sorted(mpl.selected_payloads()) == fake[2:5], str(mpl.selected_payloads()))
        check("不可见项不计入当前列表长度", mpl.size() == 2)

        fake[3].unlink()
        mpl.set_items(full[:2])
        check("文件已不存在的选中项会被清掉",
              fake[3] not in mpl.selected_payloads() and mpl.selected_count() == 2,
              str(mpl.selected_payloads()))

        mpl.set_items(full[2:], keep_selection=False)
        check("keep_selection=False 整体切换时清空选中", mpl.selected_count() == 0)
        check("换内容后锚点重置", mpl._anchor is None)
        check("每次点击都通知了界面", len(fired) >= 8, str(len(fired)))

        rm = zb.MultiPickList(root, columns=cols, height=5, remove_on_click=True)
        rm.set_items(full[:3])
        rm.set_selection([p for _l, p in full[:3]])
        rm.click_at(1)
        check("remove_on_click：点一下移出该项",
              sorted(rm.selected_payloads()) == [fake[0], fake[2]], str(rm.selected_payloads()))
        # 锚点停在刚点过的 pick1(下标1)，Shift 点下标0 → 移出 0..1
        rm.click_at(0, shift=True)
        check("remove_on_click：Shift 移出锚点到该点的区间",
              rm.selected_payloads() == [fake[2]], str(rm.selected_payloads()))
        root.destroy()
    except Exception as exc:  # 无图形会话时跳过，不影响其余断言
        print(f"  [SKIP] 无法初始化 Tk（{exc!r}），跳过选中语义测试")

    # ---------------------------------------------------------------- 清单解析
    print("\n== 目录包解析（清单优先，覆盖不到再逐个读包）==")
    _c, b20 = unpack_manifest(only / "20260920index.7z")
    check("清单带一行机器可读数据",
          any(l.startswith(zb.MANIFEST_JSON_TAG) for l in b20.splitlines()))
    m20 = zb.parse_manifest(b20)
    check("解析出当天 2 个包的映射",
          sorted(m20) == ["2026092001.7z", "2026092002.7z"], str(sorted(m20)))
    check("解析出包内原文件名", m20["2026092001.7z"][0] == ["s1.txt"], str(m20.get("2026092001.7z")))
    check("解析出打包时间（用于判断清单是否过期）",
          bool(m20["2026092001.7z"][1]), str(m20.get("2026092001.7z")))

    _c, b21 = unpack_manifest(only / "20260921index.7z")
    m21 = zb.parse_manifest(b21)
    check("多文件包解析出两个名字",
          sorted(m21["2026092102.7z"][0]) == ["x1.txt", "x2.txt"], str(m21.get("2026092102.7z")))
    check("名字多于 3 个时按省略形式显示",
          zb.summarize_names(["a", "b", "c", "d"]) == "a、b、c 等 4 个文件",
          zb.summarize_names(["a", "b", "c", "d"]))

    # 旧版本生成的清单没有那行数据，要靠人看的表格兜底
    legacy = "\n".join(l for l in b20.splitlines() if not l.startswith(zb.MANIFEST_JSON_TAG))
    m_legacy = zb.parse_manifest(legacy)
    check("旧格式清单也能解析出映射",
          sorted(m_legacy) == ["2026092001.7z", "2026092002.7z"], str(sorted(m_legacy)))
    check("旧格式解析出的名字正确",
          m_legacy["2026092002.7z"][0] == ["s2.txt"], str(m_legacy.get("2026092002.7z")))

    check("read_day_manifest 读出当天映射",
          sorted(zb.read_day_manifest(Z.inst, only, "20260920")) == ["2026092001.7z", "2026092002.7z"])
    check("没有清单的日期返回空", zb.read_day_manifest(Z.inst, only, "20260930") == {})
    check("密码不对时返回空（会退回逐个读包）",
          zb.read_day_manifest(zb.SevenZip(exe, "错的密码"), only, "20260920") == {})

    # 过期检测：清单是打包那一刻的快照，包在那之后被改动过就不能再信
    p1 = only / "2026092001.7z"
    before = zb.fmt_time(p1.stat().st_mtime)
    check("清单记录的打包时间与包当前 mtime 一致", m20["2026092001.7z"][1] == before,
          f"{m20['2026092001.7z'][1]} vs {before}")
    st = p1.stat()
    os.utime(p1, (st.st_atime, st.st_mtime + 120))
    check("包被改动后打包时间对不上（据此退回逐个读）",
          m20["2026092001.7z"][1] != zb.fmt_time(p1.stat().st_mtime))

    # ---------------------------------------------------------------- 扫描排除规则
    print("\n== 扫描排除规则（点开头的项目配置）==")
    rule = ROOT / "scan-rule"
    rule.mkdir(parents=True)
    (rule / "季度报告.docx").write_text("real\n", encoding="utf-8")
    for n in (".env", ".editorconfig", ".gitignore", ".gitattributes", "LICENSE", "README.md"):
        (rule / n).write_text("x\n", encoding="utf-8")

    got = sorted(p.name for p in zb.scan_candidates(rule, False))
    check("默认只扫到真实文件", got == ["季度报告.docx"], str(got))
    got2 = sorted(p.name for p in zb.scan_candidates(rule, False, True))
    check("勾选「包含点开头的文件」后 .env 之类可归档",
          ".env" in got2 and ".editorconfig" in got2, str(got2))
    check("但工具/项目自身的文件仍然不碰（.gitignore/.gitattributes/LICENSE/README）",
          not any(n in got2 for n in (".gitignore", ".gitattributes", "LICENSE", "README.md")),
          str(got2))
    check("is_dotfile 只认真正的点开头文件",
          zb.is_dotfile(".env") and zb.is_dotfile(".gitattributes")
          and not zb.is_dotfile("env") and not zb.is_dotfile("季度报告.docx"))
    check("项目根上的 .gitattributes 不会被当成素材",
          not any(p.name == ".gitattributes" for p in zb.scan_candidates(only, True)))

    # ---------------------------------------------------------------- 工作目录初始化
    print("\n== 工作目录初始化（回归：Path('') 等于 Path('.')）==")
    check("Path('').is_dir() 居然为真 —— 这就是当初把目录跑偏的坑", Path("").is_dir())
    check("空配置回落到默认位置", zb.initial_workdir({}) == zb.default_workdir())
    check("workdir 为空串时回落（而不是变成当前目录）",
          zb.initial_workdir({"workdir": ""}) == zb.default_workdir())
    check("workdir 只有空白字符时同样回落",
          zb.initial_workdir({"workdir": "   "}) == zb.default_workdir())
    check("workdir 指向不存在的路径时回落",
          zb.initial_workdir({"workdir": str(ROOT / "no-such-dir")}) == zb.default_workdir())
    check("workdir 合法时沿用它",
          zb.initial_workdir({"workdir": str(WORK)}) == WORK)
    check("从别的当前目录构造也不会跑偏（不依赖进程 cwd）",
          zb.initial_workdir({}) != Path("."))

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
